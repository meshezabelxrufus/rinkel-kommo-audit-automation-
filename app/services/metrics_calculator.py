"""
MetricsCalculator — computes per-agent performance metrics from unified profiles.

PURPOSE
-------
Accepts an AgentUnifiedProfile (Kommo leads + Rinkel calls merged) and
produces a typed AgentMetrics object with Kommo metrics, Rinkel metrics,
and cross-system derived scores.

CONVERSION SIGNALS (from real Kommo data analysis)
---------------------------------------------------
A lead is classified as:

  CONVERTED:
    - status_id maps to a stage with is_editable=False AND stage name
      contains a positive outcome keyword (e.g. "ganado", "won", "booked"),
    - OR: closed_at IS NOT NULL AND loss_reason_id IS NULL

  LOST:
    - loss_reason_id IS NOT NULL (explicit loss reason set),
    - OR: status_id maps to a stage with is_editable=False AND stage name
      contains a negative outcome keyword (e.g. "perdido", "cancelada"),
    - OR: closed_at IS NOT NULL AND loss_reason_id IS NOT NULL

  ACTIVE (in pipeline):
    - Everything else.

The stage_map is optional. Without it, the engine falls back to
closed_at / loss_reason_id signals alone — still functional and accurate.

METRICS PRODUCED
----------------
Kommo metrics:
  total_leads          int     — all leads in the profile
  converted_leads      int     — leads classified as won
  lost_leads           int     — leads classified as lost
  active_leads         int     — leads still in pipeline
  conversion_rate      float   — converted / total_leads (0.0–1.0), 0.0 if no leads

Rinkel metrics:
  total_calls          int     — total call records in profile
  avg_call_duration    float   — mean duration in seconds (0.0 if no calls/no duration)
  inbound_calls        int     — calls with direction "inbound" / "in" / "incoming"
  outbound_calls       int     — calls with direction "outbound" / "out" / "outgoing"

Cross-system metrics:
  leads_to_calls_ratio     float  — total_calls / total_leads, 0.0 if no leads
  call_coverage_rate       float  — fraction of leads with at least one call (0.0–1.0)
  activity_consistency     float  — penalises agents with leads but no calls
                                    1.0 if both systems have data; 0.5 if one-sided
  responsiveness_proxy     float  — conversion_rate weighted by call coverage
  engagement_score         float  — composite 0.0–1.0:
                                    0.4 * conversion_rate
                                  + 0.4 * min(leads_to_calls_ratio, 1.0)
                                  + 0.2 * activity_consistency

USAGE
-----
    from app.services.metrics_calculator import MetricsCalculator
    from app.services.kommo_audit_service import KommoAuditService

    svc = KommoAuditService(rinkel_calls=rinkel_calls)
    calculator = MetricsCalculator(stage_map=svc._provider.stages_by_id())

    for profile in svc.agent_profiles():
        metrics = calculator.calculate(profile)
        print(metrics.agent_id, metrics.conversion_rate, metrics.engagement_score)

    # Or calculate for all agents at once:
    all_metrics = calculator.calculate_many(svc.agent_profiles())
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.services.agent_linking_engine import AgentUnifiedProfile

logger = logging.getLogger(__name__)

# ── Stage outcome keyword sets ─────────────────────────────────────────────────
# Matched case-insensitively against stage_name

_WON_KEYWORDS = frozenset([
    "ganado", "ganados", "won", "win", "success", "closed", "opgelost",
    "confirmed", "booked", "confirmado", "cerrado", "vendido", "venta",
    "convertido", "deal", "sold",
])
_LOST_KEYWORDS = frozenset([
    "perdido", "perdidos", "lost", "loss", "cancelada", "cancelado",
    "canceladas", "cancel", "archivado", "archivada", "archivo",
    "churned", "rejected", "rechazado",
])

# ── Call direction normalisation ───────────────────────────────────────────────
_INBOUND_VALUES  = frozenset(["inbound", "in", "incoming"])
_OUTBOUND_VALUES = frozenset(["outbound", "out", "outgoing"])


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentMetrics:
    """
    Performance metrics for a single agent.

    All numeric fields are safe defaults (0 / 0.0) when input data is absent.
    All float fields are rounded to 4 decimal places for determinism.
    """

    # Identity
    agent_id: str

    # Kommo metrics
    total_leads: int
    converted_leads: int
    lost_leads: int
    active_leads: int
    conversion_rate: float           # 0.0 – 1.0

    # Rinkel metrics
    total_calls: int
    avg_call_duration: float         # seconds
    inbound_calls: int
    outbound_calls: int

    # Cross-system metrics
    leads_to_calls_ratio: float      # calls per lead
    call_coverage_rate: float        # fraction of leads with ≥1 call
    activity_consistency: float      # 0.0 – 1.0
    responsiveness_proxy: float      # 0.0 – 1.0
    engagement_score: float          # 0.0 – 1.0  (primary KPI)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain serialisable dict."""
        return {
            "agent_id":             self.agent_id,
            # Kommo
            "total_leads":          self.total_leads,
            "converted_leads":      self.converted_leads,
            "lost_leads":           self.lost_leads,
            "active_leads":         self.active_leads,
            "conversion_rate":      self.conversion_rate,
            # Rinkel
            "total_calls":          self.total_calls,
            "avg_call_duration":    self.avg_call_duration,
            "inbound_calls":        self.inbound_calls,
            "outbound_calls":       self.outbound_calls,
            # Cross
            "leads_to_calls_ratio": self.leads_to_calls_ratio,
            "call_coverage_rate":   self.call_coverage_rate,
            "activity_consistency": self.activity_consistency,
            "responsiveness_proxy": self.responsiveness_proxy,
            "engagement_score":     self.engagement_score,
        }

    def __repr__(self) -> str:
        return (
            f"AgentMetrics(agent_id={self.agent_id!r}, "
            f"leads={self.total_leads}, calls={self.total_calls}, "
            f"conversion={self.conversion_rate:.2%}, "
            f"engagement={self.engagement_score:.4f})"
        )


# ── Calculator ────────────────────────────────────────────────────────────────

class MetricsCalculator:
    """
    Computes AgentMetrics from AgentUnifiedProfile objects.

    Parameters
    ----------
    stage_map : dict[int, dict] | None
        Optional stage lookup from KommoProvider.stages_by_id().
        When provided, stage names are used to classify converted/lost leads.
        When absent, only closed_at / loss_reason_id signals are used.

    won_stage_ids : set[int] | None
        Optional explicit set of stage IDs to treat as "won".
        Overrides keyword matching for those IDs.

    lost_stage_ids : set[int] | None
        Optional explicit set of stage IDs to treat as "lost".
        Overrides keyword matching for those IDs.
    """

    def __init__(
        self,
        stage_map: dict[int, dict[str, Any]] | None = None,
        won_stage_ids: set[int] | None = None,
        lost_stage_ids: set[int] | None = None,
    ) -> None:
        self._stage_map: dict[int, dict[str, Any]] = stage_map or {}
        self._won_stage_ids: set[int] = won_stage_ids or set()
        self._lost_stage_ids: set[int] = lost_stage_ids or set()

        # Pre-classify all stages for performance (O(1) lookup per lead)
        self._won_ids: set[int] = set()
        self._lost_ids: set[int] = set()
        self._classify_stages()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def calculate(self, profile: Any) -> AgentMetrics:
        """
        Compute AgentMetrics for a single AgentUnifiedProfile.

        Never raises — returns all-zero metrics if profile is invalid.

        Args:
            profile: AgentUnifiedProfile instance.

        Returns:
            AgentMetrics (frozen dataclass, all floats rounded to 4dp).
        """
        if not isinstance(profile, AgentUnifiedProfile):
            logger.warning(
                "MetricsCalculator.calculate: expected AgentUnifiedProfile",
                extra={"type": type(profile).__name__},
            )
            return _null_metrics("<invalid>")

        try:
            return self._compute(profile)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "MetricsCalculator.calculate failed",
                extra={"agent_id": getattr(profile, "agent_id", "?"), "error": str(exc)},
                exc_info=True,
            )
            return _null_metrics(str(getattr(profile, "agent_id", "<unknown>")))

    def calculate_many(
        self,
        profiles: Any,
    ) -> list[AgentMetrics]:
        """
        Compute AgentMetrics for a list of AgentUnifiedProfile objects.

        Preserves the input order.  Failed profiles produce null metrics
        and do not interrupt the batch.

        Args:
            profiles: Iterable of AgentUnifiedProfile.

        Returns:
            list[AgentMetrics] — same length as input.
        """
        if not hasattr(profiles, "__iter__"):
            logger.warning(
                "MetricsCalculator.calculate_many: non-iterable input",
                extra={"type": type(profiles).__name__},
            )
            return []
        return [self.calculate(p) for p in profiles]

    # ─────────────────────────────────────────────────────────────────────────
    # Core computation
    # ─────────────────────────────────────────────────────────────────────────

    def _compute(self, profile: AgentUnifiedProfile) -> AgentMetrics:
        # ── Kommo metrics ──────────────────────────────────────────────────
        total_leads = len(profile.kommo_leads)
        converted = lost = 0

        for lead in profile.kommo_leads:
            outcome = self._classify_lead(lead)
            if outcome == "won":
                converted += 1
            elif outcome == "lost":
                lost += 1

        active_leads = total_leads - converted - lost
        conversion_rate = _safe_divide(converted, total_leads)

        # ── Rinkel metrics ─────────────────────────────────────────────────
        total_calls = len(profile.rinkel_calls)
        inbound = outbound = 0
        durations: list[float] = []

        for call in profile.rinkel_calls:
            if not isinstance(call, dict):
                continue
            direction = str(call.get("direction") or call.get("call_direction") or "").lower().strip()
            if direction in _INBOUND_VALUES:
                inbound += 1
            elif direction in _OUTBOUND_VALUES:
                outbound += 1

            # Duration: try multiple field names
            dur = (
                call.get("duration")
                or call.get("duration_seconds")
                or call.get("call_duration")
                or 0
            )
            if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
                durations.append(float(dur))

        avg_duration = _safe_divide(sum(durations), len(durations)) if durations else 0.0

        # ── Cross-system metrics ───────────────────────────────────────────

        # leads_to_calls_ratio — how many calls per lead
        leads_to_calls_ratio = _safe_divide(total_calls, total_leads)

        # call_coverage_rate — fraction of leads with ≥1 associated call
        # (we can compute this if lead IDs appear in calls, but here we use
        # the simpler proxy: min(calls/leads, 1.0))
        call_coverage_rate = min(leads_to_calls_ratio, 1.0) if total_leads > 0 else (
            1.0 if total_calls > 0 else 0.0
        )

        # activity_consistency — penalises one-sided agents
        if total_leads > 0 and total_calls > 0:
            activity_consistency = 1.0          # both systems active
        elif total_leads > 0 or total_calls > 0:
            activity_consistency = 0.5          # one-sided
        else:
            activity_consistency = 0.0          # no data

        # responsiveness_proxy — conversion rate weighted by call coverage
        responsiveness_proxy = _round4(conversion_rate * call_coverage_rate)

        # engagement_score — composite KPI (0.0–1.0)
        engagement_score = _round4(
            0.40 * conversion_rate
            + 0.40 * min(leads_to_calls_ratio, 1.0)
            + 0.20 * activity_consistency
        )

        return AgentMetrics(
            agent_id=profile.agent_id,
            # Kommo
            total_leads=total_leads,
            converted_leads=converted,
            lost_leads=lost,
            active_leads=max(active_leads, 0),
            conversion_rate=_round4(conversion_rate),
            # Rinkel
            total_calls=total_calls,
            avg_call_duration=_round4(avg_duration),
            inbound_calls=inbound,
            outbound_calls=outbound,
            # Cross
            leads_to_calls_ratio=_round4(leads_to_calls_ratio),
            call_coverage_rate=_round4(call_coverage_rate),
            activity_consistency=_round4(activity_consistency),
            responsiveness_proxy=responsiveness_proxy,
            engagement_score=engagement_score,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Lead classification
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_lead(self, lead: Any) -> str:
        """
        Classify a single lead as "won", "lost", or "active".

        Priority order (highest to lowest):
          1. loss_reason_id IS NOT NULL → always "lost" (explicit CRM loss signal)
          2. Explicit lost_stage_ids override → "lost"
          3. Explicit won_stage_ids override → "won"
          4. Stage map keyword classification (non-editable stages only)
          5. closed_at IS NOT NULL AND no loss reason → "won"
          6. Default → "active"

        loss_reason_id is checked first because it is the most explicit signal
        in Kommo — even a "Leads ganados" stage can have a loss reason if the
        lead was re-opened and re-closed as a loss.

        Returns: "won" | "lost" | "active"
        """
        if not isinstance(lead, dict):
            return "active"

        closed_at   = lead.get("closed_at")
        loss_reason = lead.get("loss_reason_id")

        has_closed = closed_at   is not None and not isinstance(closed_at,   bool)
        has_loss   = loss_reason is not None and not isinstance(loss_reason, bool)

        # Priority 1: explicit loss signal always wins
        if has_loss:
            return "lost"

        status_id = lead.get("status_id")
        sid = (
            int(status_id)
            if isinstance(status_id, (int, float)) and not isinstance(status_id, bool)
            else None
        )

        # Priority 2: explicit lost_stage_ids constructor override
        if sid is not None and sid in self._lost_ids:
            return "lost"

        # Priority 3: explicit won_stage_ids constructor override
        if sid is not None and sid in self._won_ids:
            return "won"

        # Priority 5: closed without a loss reason → won
        if has_closed:
            return "won"

        return "active"

    # ─────────────────────────────────────────────────────────────────────────
    # Stage pre-classification
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_stages(self) -> None:
        """
        Pre-classify all stages from the stage_map into won/lost sets.

        Uses keyword matching on stage_name, with explicit ID overrides
        taking priority.
        """
        for stage_id, stage in self._stage_map.items():
            if not isinstance(stage, dict):
                continue

            # Explicit overrides always win
            if stage_id in self._won_stage_ids:
                self._won_ids.add(stage_id)
                continue
            if stage_id in self._lost_stage_ids:
                self._lost_ids.add(stage_id)
                continue

            name = str(stage.get("stage_name") or "").lower()
            is_editable = stage.get("is_editable", True)

            # Only classify terminal (non-editable) stages by keyword
            # Editable stages are "active" regardless of name
            if not is_editable:
                if any(kw in name for kw in _WON_KEYWORDS):
                    self._won_ids.add(stage_id)
                elif any(kw in name for kw in _LOST_KEYWORDS):
                    self._lost_ids.add(stage_id)

        logger.debug(
            "MetricsCalculator: stage classification complete",
            extra={
                "won_stage_ids":  sorted(self._won_ids),
                "lost_stage_ids": sorted(self._lost_ids),
            },
        )


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _safe_divide(numerator: float, denominator: float) -> float:
    """Divide numerator by denominator; return 0.0 on zero/invalid denominator."""
    try:
        if denominator == 0 or not math.isfinite(denominator):
            return 0.0
        result = numerator / denominator
        return result if math.isfinite(result) else 0.0
    except (TypeError, ZeroDivisionError):
        return 0.0


def _round4(value: float) -> float:
    """Round to 4 decimal places for determinism."""
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _null_metrics(agent_id: str) -> AgentMetrics:
    """Return an all-zero AgentMetrics for invalid/failed profiles."""
    return AgentMetrics(
        agent_id=agent_id,
        total_leads=0, converted_leads=0, lost_leads=0,
        active_leads=0, conversion_rate=0.0,
        total_calls=0, avg_call_duration=0.0,
        inbound_calls=0, outbound_calls=0,
        leads_to_calls_ratio=0.0, call_coverage_rate=0.0,
        activity_consistency=0.0, responsiveness_proxy=0.0,
        engagement_score=0.0,
    )
