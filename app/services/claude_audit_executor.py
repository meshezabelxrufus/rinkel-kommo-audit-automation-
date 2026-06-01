"""
ClaudeAuditExecutor — submits agent audit context to Claude and returns
structured QA scores.

PURPOSE
-------
The final stage in the Kommo CRM audit pipeline:

  AuditEngine.run()         → list[AgentAuditReport]
        │
        ▼
  ClaudeAuditExecutor       → list[ClaudeAuditResult]

Takes each agent's performance data + normalized leads, constructs a
structured prompt, submits to Claude (claude-sonnet-4 by default), and
parses the JSON response into a typed result object.

DESIGN PRINCIPLES
-----------------
- No database, no ingestion layer, no external state
- Fully sync prompt construction; async only for API calls
- Never crashes on missing data or bad Claude responses
- Deterministic prompt construction (same input → same prompt)
- Rate-limited and retried via the Anthropic SDK
- Dry-run mode for testing without API key

OUTPUT
------
ClaudeAuditResult (per agent):
  agent_id         str
  status           "ok" | "skipped" | "error"
  prompt_tokens    int
  completion_tokens int
  model            str
  executed_at      str  (ISO)

  scores           ClaudeScores | None
    overall_score         float  (1.0–5.0)
    conversion_assessment str
    call_activity_note    str
    strengths             list[str]
    coaching_points       list[str]
    risk_level            "none"|"low"|"medium"|"high"
    data_quality          "HIGH"|"MEDIUM"|"LOW"
    raw_response          str    (full Claude JSON text)

USAGE
-----
    from app.services.claude_audit_executor import ClaudeAuditExecutor
    from app.services.audit_engine import AuditEngine

    engine   = AuditEngine(exports_dir="exports/")
    reports  = engine.run()

    executor = ClaudeAuditExecutor(api_key="sk-ant-...")
    results  = await executor.execute(reports)

    for r in results:
        if r.status == "ok":
            print(r.agent_id, r.scores.overall_score)

    # Dry-run (no API call — prompt only)
    executor = ClaudeAuditExecutor(dry_run=True)
    results  = await executor.execute(reports)

    # Single agent
    result = await executor.execute_one(reports[0])

    # Summary
    summary = executor.summarise(results)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default model — using latest Claude Sonnet
_DEFAULT_MODEL    = "claude-sonnet-4-5"
_DEFAULT_MAX_TOK  = 1500
_DEFAULT_TEMP     = 0          # deterministic
_MAX_LEADS_SHOWN  = 10         # cap leads in prompt to stay within token budget
_MAX_RETRIES      = 2
_RETRY_DELAY_S    = 2.0


# ── Output types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClaudeScores:
    """Parsed QA scores from Claude's JSON response."""
    overall_score:         float        # 1.0–5.0
    conversion_assessment: str          # brief analysis of conversion rate
    call_activity_note:    str          # brief analysis of call engagement
    strengths:             tuple[str, ...]
    coaching_points:       tuple[str, ...]
    risk_level:            str          # "none"|"low"|"medium"|"high"
    data_quality:          str          # "HIGH"|"MEDIUM"|"LOW"
    raw_response:          str          # full JSON text from Claude

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_score":         self.overall_score,
            "conversion_assessment": self.conversion_assessment,
            "call_activity_note":    self.call_activity_note,
            "strengths":             list(self.strengths),
            "coaching_points":       list(self.coaching_points),
            "risk_level":            self.risk_level,
            "data_quality":          self.data_quality,
        }


@dataclass(frozen=True)
class ClaudeAuditResult:
    """Result for a single agent audit submission."""
    agent_id:          str
    status:            str          # "ok" | "skipped" | "error"
    prompt_tokens:     int
    completion_tokens: int
    model:             str
    executed_at:       str          # ISO timestamp
    scores:            ClaudeScores | None = None
    error:             str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "agent_id":          self.agent_id,
            "status":            self.status,
            "prompt_tokens":     self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "model":             self.model,
            "executed_at":       self.executed_at,
            "error":             self.error,
            "scores":            self.scores.to_dict() if self.scores else None,
        }
        return d

    def __repr__(self) -> str:
        score = f"{self.scores.overall_score:.1f}" if self.scores else "—"
        return (
            f"ClaudeAuditResult(agent_id={self.agent_id!r}, "
            f"status={self.status!r}, score={score})"
        )


# ── Executor ──────────────────────────────────────────────────────────────────

class ClaudeAuditExecutor:
    """
    Submits agent audit reports to Claude and returns structured QA scores.

    Parameters
    ----------
    api_key : str | None
        Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.
        Ignored when dry_run=True.

    model : str
        Claude model to use. Default: claude-sonnet-4-5.

    max_tokens : int
        Max completion tokens per request. Default: 1500.

    dry_run : bool
        If True, skips API calls and returns prompt-only results
        (status="skipped"). Useful for testing without an API key.

    concurrency : int
        Max parallel Claude API calls. Default: 3 (avoids rate limits).

    skip_kommo_only : bool
        If True, skip agents with no Rinkel calls (engagement_score == 0).
        Default False — all agents are audited.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOK,
        dry_run: bool = False,
        concurrency: int = 3,
        skip_kommo_only: bool = False,
    ) -> None:
        self._model          = model
        self._max_tokens     = max_tokens
        self._dry_run        = dry_run
        self._concurrency    = concurrency
        self._skip_kommo_only = skip_kommo_only
        self._client         = None  # lazy-init to avoid import cost at module load

        if not dry_run:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(
                    api_key=api_key,   # None → reads ANTHROPIC_API_KEY from env
                    max_retries=_MAX_RETRIES,
                )
            except ImportError:
                logger.warning(
                    "anthropic package not installed — falling back to dry_run mode"
                )
                self._dry_run = True

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(
        self,
        reports: list[Any],
    ) -> list[ClaudeAuditResult]:
        """
        Execute Claude audits for all agents concurrently.

        Args:
            reports: list[AgentAuditReport] from AuditEngine.run()

        Returns:
            list[ClaudeAuditResult] in the same order as input reports.
            Never raises — errors are captured per agent (status="error").
        """
        if not reports:
            return []

        sem = asyncio.Semaphore(self._concurrency)

        async def _bounded(report):
            async with sem:
                return await self.execute_one(report)

        tasks = [_bounded(r) for r in reports]
        return list(await asyncio.gather(*tasks))

    async def execute_one(self, report: Any) -> ClaudeAuditResult:
        """
        Execute a Claude audit for a single AgentAuditReport.

        Never raises — all exceptions become status="error" results.
        """
        executed_at = datetime.now(timezone.utc).isoformat()

        # Validate input
        agent_id = _safe_agent_id(report)
        if agent_id == "<invalid>":
            return _error_result(agent_id, "invalid report object", executed_at, self._model)

        # Skip filter
        if self._skip_kommo_only and _is_kommo_only(report):
            logger.debug("ClaudeAuditExecutor: skipping kommo-only agent", extra={"agent": agent_id})
            return _skipped_result(agent_id, executed_at, self._model)

        # Build prompt
        system_prompt, user_prompt = self._build_prompt(report)

        # Dry-run: return prompt stats without calling Claude
        if self._dry_run:
            return _skipped_result(agent_id, executed_at, self._model)

        # Submit to Claude
        try:
            result = await self._call_claude(
                agent_id=agent_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                executed_at=executed_at,
            )
            logger.info(
                "ClaudeAuditExecutor: agent audited",
                extra={
                    "agent": agent_id,
                    "tokens": result.prompt_tokens + result.completion_tokens,
                    "score": result.scores.overall_score if result.scores else None,
                },
            )
            return result

        except Exception as exc:
            logger.error(
                "ClaudeAuditExecutor: API call failed",
                extra={"agent": agent_id, "error": str(exc)},
                exc_info=True,
            )
            return _error_result(agent_id, str(exc), executed_at, self._model)

    def summarise(self, results: list[ClaudeAuditResult]) -> dict[str, Any]:
        """
        Return aggregate summary of execution results.

        Returns
        -------
        dict with keys:
            total, ok, skipped, errors,
            avg_score, avg_prompt_tokens, avg_completion_tokens,
            top_performer, bottom_performer,
            total_tokens_used
        """
        if not results:
            return {
                "total": 0, "ok": 0, "skipped": 0, "errors": 0,
                "avg_score": None, "avg_prompt_tokens": 0,
                "avg_completion_tokens": 0, "total_tokens_used": 0,
                "top_performer": None, "bottom_performer": None,
            }

        ok      = [r for r in results if r.status == "ok"]
        skipped = [r for r in results if r.status == "skipped"]
        errors  = [r for r in results if r.status == "error"]

        scored = [r for r in ok if r.scores is not None]
        avg_score = (
            round(sum(r.scores.overall_score for r in scored) / len(scored), 2)
            if scored else None
        )
        top    = max(scored, key=lambda r: r.scores.overall_score, default=None)
        bottom = min(scored, key=lambda r: r.scores.overall_score, default=None)

        total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in results)
        avg_pt = round(sum(r.prompt_tokens for r in results) / len(results))
        avg_ct = round(sum(r.completion_tokens for r in results) / len(results))

        return {
            "total":               len(results),
            "ok":                  len(ok),
            "skipped":             len(skipped),
            "errors":              len(errors),
            "avg_score":           avg_score,
            "avg_prompt_tokens":   avg_pt,
            "avg_completion_tokens": avg_ct,
            "total_tokens_used":   total_tokens,
            "top_performer":       top.agent_id if top else None,
            "bottom_performer":    bottom.agent_id if bottom else None,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Prompt construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_prompt(self, report: Any) -> tuple[str, str]:
        """
        Build (system_prompt, user_prompt) for a single AgentAuditReport.

        Deterministic: same report → same prompt.
        Capped at _MAX_LEADS_SHOWN leads to control token usage.
        """
        system = _SYSTEM_PROMPT

        kommo   = getattr(report, "kommo",    None)
        rinkel  = getattr(report, "rinkel",   None)
        combined = getattr(report, "combined", None)
        leads   = getattr(report, "normalized_leads", ()) or ()

        agent_id = _safe_agent_id(report)

        # Build lead sample (capped)
        lead_lines = []
        for lead in list(leads)[:_MAX_LEADS_SHOWN]:
            status = getattr(lead, "stage_name", None) or "unknown"
            loss   = getattr(lead, "loss_reason_id", None)
            closed = getattr(lead, "closed_at_iso", None)
            lead_lines.append(
                f"  - lead_id={getattr(lead, 'id', '?')} "
                f"stage={status!r} "
                f"loss_reason={loss or 'none'} "
                f"closed_at={closed or 'open'}"
            )
        leads_block = "\n".join(lead_lines) if lead_lines else "  (none)"

        total_leads_shown = len(list(leads)[:_MAX_LEADS_SHOWN])
        total_leads_total = len(list(leads))
        truncation_note = (
            f"  [showing {total_leads_shown} of {total_leads_total} leads]"
            if total_leads_total > _MAX_LEADS_SHOWN else ""
        )

        user = f"""You are auditing agent: {agent_id}

KOMMO CRM METRICS:
  total_leads:      {getattr(kommo, 'total_leads', 0)}
  converted_leads:  {getattr(kommo, 'converted_leads', 0)}
  lost_leads:       {getattr(kommo, 'lost_leads', 0)}
  active_leads:     {getattr(kommo, 'active_leads', 0)}
  conversion_rate:  {getattr(kommo, 'conversion_rate', 0.0):.4f}

RINKEL CALL METRICS:
  total_calls:       {getattr(rinkel, 'total_calls', 0)}
  inbound_calls:     {getattr(rinkel, 'inbound_calls', 0)}
  outbound_calls:    {getattr(rinkel, 'outbound_calls', 0)}
  avg_call_duration: {getattr(rinkel, 'avg_call_duration', 0.0):.1f}s
  engagement_score:  {getattr(rinkel, 'engagement_score', 0.0):.4f}

COMBINED PERFORMANCE:
  performance_score:    {getattr(combined, 'performance_score', 0.0):.4f}
  activity_consistency: {getattr(combined, 'activity_consistency', 0.0):.4f}
  responsiveness_proxy: {getattr(combined, 'responsiveness_proxy', 0.0):.4f}
  data_sources:         kommo={getattr(combined, 'data_source_flags', {}).get('kommo', False)}, rinkel={getattr(combined, 'data_source_flags', {}).get('rinkel', False)}

LEAD SAMPLE:{truncation_note}
{leads_block}

Audit this agent and return ONLY this JSON:

{{
  "overall_score": <1.0–5.0 float>,
  "conversion_assessment": "<one sentence on conversion rate>",
  "call_activity_note": "<one sentence on call engagement — or 'No call data' if total_calls=0>",
  "strengths": ["<max 3 specific strengths based on data>"],
  "coaching_points": ["<max 3 specific improvement areas based on data>"],
  "risk_level": "<none|low|medium|high>",
  "data_quality": "<HIGH|MEDIUM|LOW>"
}}"""

        return system, user

    # ─────────────────────────────────────────────────────────────────────────
    # Claude API call
    # ─────────────────────────────────────────────────────────────────────────

    async def _call_claude(
        self,
        agent_id: str,
        system_prompt: str,
        user_prompt: str,
        executed_at: str,
    ) -> ClaudeAuditResult:
        """Submit to Claude API and parse the response."""
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=_DEFAULT_TEMP,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text       = response.content[0].text if response.content else ""
        prompt_tokens  = getattr(response.usage, "input_tokens",  0)
        comp_tokens    = getattr(response.usage, "output_tokens", 0)

        scores = _parse_scores(raw_text)

        return ClaudeAuditResult(
            agent_id=agent_id,
            status="ok" if scores else "error",
            prompt_tokens=prompt_tokens,
            completion_tokens=comp_tokens,
            model=self._model,
            executed_at=executed_at,
            scores=scores,
            error=None if scores else f"Failed to parse Claude response: {raw_text[:200]}",
        )

    def __repr__(self) -> str:
        return (
            f"ClaudeAuditExecutor("
            f"model={self._model!r}, "
            f"dry_run={self._dry_run}, "
            f"concurrency={self._concurrency})"
        )


# ── Score parser ──────────────────────────────────────────────────────────────

def _parse_scores(raw_text: str) -> ClaudeScores | None:
    """
    Parse Claude's JSON response into ClaudeScores.

    Handles:
    - Valid JSON response
    - JSON wrapped in markdown code fences (```json ... ```)
    - Partial or malformed responses → returns None

    Never raises.
    """
    if not raw_text or not raw_text.strip():
        return None

    # Strip markdown fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = [l for l in lines if not l.startswith("```")]
        text = "\n".join(inner).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Try to extract JSON object with regex
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(data, dict):
        return None

    try:
        return ClaudeScores(
            overall_score=         float(data.get("overall_score", 0)),
            conversion_assessment= str(data.get("conversion_assessment", "")),
            call_activity_note=    str(data.get("call_activity_note", "")),
            strengths=             tuple(data.get("strengths") or []),
            coaching_points=       tuple(data.get("coaching_points") or []),
            risk_level=            str(data.get("risk_level", "none")),
            data_quality=          str(data.get("data_quality", "LOW")),
            raw_response=          raw_text,
        )
    except (TypeError, ValueError):
        return None


# ── Private helpers ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a senior performance analyst auditing call-centre agents.
You receive CRM metrics (Kommo) and call activity metrics (Rinkel) for a single agent.

RULES:
1. Base ALL analysis on the numeric data provided. Do not invent information.
2. If total_calls = 0, the agent has no call data — note this factually.
3. overall_score: 1=very poor, 2=poor, 3=average, 4=good, 5=excellent.
4. risk_level: based on conversion_rate and performance_score (high = score < 0.1).
5. data_quality: HIGH = both kommo+rinkel data, MEDIUM = one system only, LOW = minimal data.
6. Return ONLY the requested JSON object. No preamble, no commentary."""


def _safe_agent_id(report: Any) -> str:
    """Safely extract agent_id from any object."""
    try:
        return str(report.agent_id)
    except AttributeError:
        return "<invalid>"


def _is_kommo_only(report: Any) -> bool:
    """Return True if the agent has no Rinkel calls."""
    try:
        return report.rinkel.total_calls == 0
    except AttributeError:
        return True


def _skipped_result(agent_id: str, executed_at: str, model: str) -> ClaudeAuditResult:
    return ClaudeAuditResult(
        agent_id=agent_id,
        status="skipped",
        prompt_tokens=0,
        completion_tokens=0,
        model=model,
        executed_at=executed_at,
        scores=None,
        error=None,
    )


def _error_result(agent_id: str, error: str, executed_at: str, model: str) -> ClaudeAuditResult:
    return ClaudeAuditResult(
        agent_id=agent_id,
        status="error",
        prompt_tokens=0,
        completion_tokens=0,
        model=model,
        executed_at=executed_at,
        scores=None,
        error=error,
    )
