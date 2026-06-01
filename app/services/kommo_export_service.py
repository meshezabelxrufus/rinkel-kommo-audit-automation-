"""
KommoExportService — JSONL exporter for the Kommo CRM audit layer.

PURPOSE
-------
Bridges the Kommo data layer (KommoAuditService + MetricsCalculator) to the
existing Claude audit pipeline by producing JSONL exports in the same format
as ExportService, but sourced entirely from local Kommo JSON exports rather
than the Rinkel/Supabase DB.

Each JSONL line is one agent record: profile + metrics + normalized leads
+ Rinkel calls — ready to feed into audit_prompts.py prompt builders.

DESIGN PRINCIPLES
-----------------
- Read-only and stateless (same as all Kommo-layer services)
- No async/await — synchronous, no I/O except file writes
- Deterministic output for same input
- SHA-256 checksum on every export file
- Compatible with existing audit_prompts.build_batch_scoring_prompt()
- Fault-tolerant — failed agents are logged and skipped, never crash

OUTPUT FORMAT (per line)
------------------------
Each JSONL line is a JSON object:

{
  "agent_id":         str,        -- Kommo responsible_user_id
  "export_schema":    "kommo_v1",
  "exported_at":      ISO str,

  "metrics": {
    "total_leads":          int,
    "converted_leads":      int,
    "lost_leads":           int,
    "active_leads":         int,
    "conversion_rate":      float,
    "total_calls":          int,
    "avg_call_duration":    float,
    "inbound_calls":        int,
    "outbound_calls":       int,
    "leads_to_calls_ratio": float,
    "call_coverage_rate":   float,
    "activity_consistency": float,
    "responsiveness_proxy": float,
    "engagement_score":     float,
  },

  "normalized_leads": [...],     -- NormalizedLead.to_dict() per lead
  "rinkel_calls":     [...],     -- raw Rinkel call dicts

  "pipeline_summary": [...]      -- pipelines the agent works in
}

USAGE
-----
    from app.services.kommo_export_service import KommoExportService

    exporter = KommoExportService(
        exports_dir="exports/",
        output_dir="exports/jsonl/",
        rinkel_calls=rinkel_call_list,         # optional
        agent_id_map={"sophie": "10359915"},    # optional
    )

    # Dry-run preview
    preview = exporter.preview()
    # {"total_agents": 2, "total_leads": 448, "total_calls": 0, ...}

    # Write JSONL file to disk
    result = exporter.export()
    # {"file_path": "...", "records_written": 2, "checksum": "...", ...}

    # Stream JSONL lines (for HTTP / further processing)
    for line in exporter.stream():
        print(line)

    # Get records as Python dicts (no file I/O)
    records = exporter.to_records()
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from app.integrations.kommo import KommoProvider
from app.services.agent_linking_engine import AgentUnifiedProfile
from app.services.kommo_audit_service import KommoAuditService
from app.services.metrics_calculator import AgentMetrics, MetricsCalculator
from app.services.lead_normalizer import LeadNormalizer

logger = logging.getLogger(__name__)

# Schema version tag embedded in every JSONL line
EXPORT_SCHEMA = "kommo_v1"

# Default output directory (relative to project root)
DEFAULT_OUTPUT_DIR = "exports/jsonl"


class KommoExportService:
    """
    Synchronous JSONL exporter for the Kommo CRM audit layer.

    Parameters
    ----------
    exports_dir : str | None
        Path to the Kommo JSON exports directory.
        Defaults to KommoProvider's default (exports/).

    output_dir : str | None
        Directory to write JSONL files to.
        Defaults to exports/jsonl/.

    rinkel_calls : list[dict] | None
        Optional Rinkel call records to include in the export.

    agent_id_map : dict[str, str] | None
        Optional cross-reference {rinkel_agent_id → kommo_user_id}.

    include_kommo_only : bool
        If True (default), include agents with Kommo leads but no calls.

    include_rinkel_only : bool
        If True (default), include agents with calls but no Kommo leads.

    min_engagement_score : float | None
        If set, only export agents with engagement_score >= this value.
        Useful for targeted audits (e.g. only bottom quartile).
    """

    def __init__(
        self,
        exports_dir: str | None = None,
        output_dir: str | None = None,
        rinkel_calls: list[dict[str, Any]] | None = None,
        agent_id_map: dict[str, str] | None = None,
        include_kommo_only: bool = True,
        include_rinkel_only: bool = True,
        min_engagement_score: float | None = None,
    ) -> None:
        self._svc = KommoAuditService(
            exports_dir=exports_dir,
            rinkel_calls=rinkel_calls or [],
            agent_id_map=agent_id_map or {},
        )
        provider = KommoProvider(exports_dir)
        self._calculator = MetricsCalculator(
            stage_map=provider.stages_by_id()
        )
        self._normalizer = LeadNormalizer(
            stage_map=provider.stages_by_id()
        )
        self._output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self._include_kommo_only = include_kommo_only
        self._include_rinkel_only = include_rinkel_only
        self._min_score = min_engagement_score

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def preview(self) -> dict[str, Any]:
        """
        Return a count-only preview without writing any files.

        Returns
        -------
        dict with keys:
            total_agents          int — agents that would be exported
            total_leads           int — total leads across all agents
            total_calls           int — total calls across all agents
            matched_agents        int — agents in both systems
            kommo_only_agents     int
            rinkel_only_agents    int
            estimated_size_bytes  int — rough estimate (1200 bytes/record)
            filters               dict — active filter settings
        """
        profiles = self._filtered_profiles()
        metrics  = self._calculator.calculate_many(profiles)

        total_leads = sum(p.total_leads for p in profiles)
        total_calls = sum(p.total_calls for p in profiles)
        matched     = sum(1 for p in profiles if p.is_matched)
        k_only      = sum(1 for p in profiles if p.is_kommo_only)
        r_only      = sum(1 for p in profiles if p.is_rinkel_only)

        return {
            "total_agents":         len(profiles),
            "total_leads":          total_leads,
            "total_calls":          total_calls,
            "matched_agents":       matched,
            "kommo_only_agents":    k_only,
            "rinkel_only_agents":   r_only,
            "estimated_size_bytes": len(profiles) * 1200,
            "filters": {
                "include_kommo_only":  self._include_kommo_only,
                "include_rinkel_only": self._include_rinkel_only,
                "min_engagement_score": self._min_score,
            },
        }

    def export(
        self,
        *,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """
        Write a JSONL export file to output_dir.

        Each line is one agent record (see module docstring for format).
        Includes SHA-256 checksum and timing.

        Args:
            filename: Override the generated filename.

        Returns
        -------
        dict with keys:
            file_path          str
            records_written    int
            total_leads        int
            total_calls        int
            file_size_bytes    int
            checksum           str  (SHA-256)
            duration_ms        int
            exported_at        str  (ISO)
        """
        start = time.perf_counter()
        now_iso = _now_iso()

        self._output_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"kommo_audit_{ts}.jsonl"
        file_path = self._output_dir / filename

        sha256 = hashlib.sha256()
        records_written = 0
        total_leads = total_calls = 0

        try:
            with open(file_path, "w", encoding="utf-8") as fh:
                for record in self._generate_records(now_iso=now_iso):
                    line = json.dumps(record, ensure_ascii=False) + "\n"
                    fh.write(line)
                    sha256.update(line.encode("utf-8"))
                    records_written += 1
                    total_leads += record.get("metrics", {}).get("total_leads", 0)
                    total_calls += record.get("metrics", {}).get("total_calls", 0)

        except Exception as exc:
            logger.error(
                "KommoExportService.export failed",
                extra={"file_path": str(file_path), "error": str(exc)},
                exc_info=True,
            )
            raise

        file_size  = file_path.stat().st_size
        checksum   = sha256.hexdigest()
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        logger.info(
            "KommoExportService.export complete",
            extra={
                "file":     str(file_path),
                "records":  records_written,
                "leads":    total_leads,
                "calls":    total_calls,
                "bytes":    file_size,
                "ms":       elapsed_ms,
            },
        )

        return {
            "file_path":       str(file_path),
            "records_written": records_written,
            "total_leads":     total_leads,
            "total_calls":     total_calls,
            "file_size_bytes": file_size,
            "checksum":        checksum,
            "duration_ms":     elapsed_ms,
            "exported_at":     now_iso,
        }

    def stream(self) -> Generator[str, None, None]:
        """
        Yield JSONL lines (str, newline-terminated) without writing to disk.

        Useful for HTTP streaming responses or piping to other services.
        """
        now_iso = _now_iso()
        for record in self._generate_records(now_iso=now_iso):
            yield json.dumps(record, ensure_ascii=False) + "\n"

    def to_records(self) -> list[dict[str, Any]]:
        """
        Return all export records as Python dicts (no file I/O).

        Useful for unit testing, in-memory processing, and audit prompts.
        """
        now_iso = _now_iso()
        return list(self._generate_records(now_iso=now_iso))

    # ─────────────────────────────────────────────────────────────────────────
    # Record generation
    # ─────────────────────────────────────────────────────────────────────────

    def _generate_records(
        self,
        now_iso: str,
    ) -> Generator[dict[str, Any], None, None]:
        """
        Yield one export record dict per agent.

        Skips agents that fail to build — never raises.
        """
        profiles = self._filtered_profiles()

        for profile in profiles:
            try:
                record = self._build_record(profile, exported_at=now_iso)
                if record is not None:
                    yield record
            except Exception as exc:
                logger.error(
                    "KommoExportService: failed to build record",
                    extra={"agent_id": profile.agent_id, "error": str(exc)},
                    exc_info=True,
                )

    def _build_record(
        self,
        profile: AgentUnifiedProfile,
        exported_at: str,
    ) -> dict[str, Any] | None:
        """
        Build a single JSONL export record for an agent.

        Returns None if the profile is effectively empty.
        """
        metrics: AgentMetrics = self._calculator.calculate(profile)

        # Normalise this agent's leads (with stage name resolution)
        norm_leads = [
            self._normalizer.normalize(lead).to_dict()
            for lead in profile.kommo_leads
        ]

        # Pipeline summary
        pipeline_map = self._svc._provider.pipelines_by_id()
        pipeline_ids = {
            lead.get("pipeline_id")
            for lead in profile.kommo_leads
            if isinstance(lead.get("pipeline_id"), int)
        }
        pipeline_summary = []
        for pid in sorted(pipeline_ids):
            p = pipeline_map.get(pid)
            if p:
                pipeline_summary.append({
                    "pipeline_id":   pid,
                    "pipeline_name": p.get("pipeline_name"),
                    "total_stages":  p.get("total_stages"),
                })

        return {
            "agent_id":       profile.agent_id,
            "export_schema":  EXPORT_SCHEMA,
            "exported_at":    exported_at,
            "metrics":        metrics.to_dict(),
            "normalized_leads": norm_leads,
            "rinkel_calls":   profile.rinkel_calls,
            "pipeline_summary": pipeline_summary,
        }

    def _filtered_profiles(self) -> list[AgentUnifiedProfile]:
        """
        Return agent profiles after applying export filters.
        """
        profiles = self._svc.agent_profiles()

        # Optionally drop kommo-only or rinkel-only agents
        if not self._include_kommo_only:
            profiles = [p for p in profiles if not p.is_kommo_only]
        if not self._include_rinkel_only:
            profiles = [p for p in profiles if not p.is_rinkel_only]

        # Drop empty profiles
        profiles = [p for p in profiles if p.total_leads + p.total_calls > 0]

        # Engagement score filter
        if self._min_score is not None:
            filtered = []
            for p in profiles:
                m = self._calculator.calculate(p)
                if m.engagement_score >= self._min_score:
                    filtered.append(p)
            profiles = filtered

        return profiles

    # ─────────────────────────────────────────────────────────────────────────
    # Audit prompt integration
    # ─────────────────────────────────────────────────────────────────────────

    def build_batch_prompt(
        self,
        agent_id: str,
        *,
        max_calls: int = 5,
    ) -> tuple[str, str] | None:
        """
        Build a batch scoring prompt for a single agent (for Claude).

        Wraps audit_prompts.build_batch_scoring_prompt() with Kommo data.

        Args:
            agent_id: Agent to build prompt for.
            max_calls: Max Rinkel calls to include in the batch (token budget).

        Returns:
            (system_prompt, user_prompt) tuple, or None if agent not found.
        """
        from app.services.audit_prompts import build_batch_scoring_prompt

        profile = self._svc.agent_profile(agent_id)
        if profile is None:
            return None

        metrics = self._calculator.calculate(profile)

        # Use Rinkel calls as the "records" for the batch prompt
        # Convert call dicts to the minimal format expected by audit_prompts
        call_records = []
        for call in profile.rinkel_calls[:max_calls]:
            call_records.append({
                "call_id":          call.get("call_id", "unknown"),
                "direction":        call.get("direction", "unknown"),
                "duration_seconds": call.get("duration", 0),
                "started_at":       call.get("started_at") or call.get("timestamp_iso", "unknown"),
                "transcript": {
                    "content":          call.get("transcript") or "[NO TRANSCRIPT]",
                    "confidence_score": call.get("confidence_score"),
                    "segments":         call.get("segments"),
                },
            })

        if not call_records:
            return None

        system, user = build_batch_scoring_prompt(
            agent_name=f"Agent {agent_id}",
            agent_id=agent_id,
            date_range=f"engagement_score={metrics.engagement_score:.4f}",
            records=call_records,
        )
        return system, user

    def __repr__(self) -> str:
        files = self._svc.available_files()
        present = [k for k, v in files.items() if v]
        return (
            f"KommoExportService("
            f"output_dir={str(self._output_dir)!r}, "
            f"files={present})"
        )


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
