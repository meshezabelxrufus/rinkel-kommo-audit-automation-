"""
AuditEngine — top-level pipeline orchestrating the full CRM + call audit workflow.

PIPELINE (7 steps)
------------------
1. Fetch Kommo leads         KommoProvider.get_leads()
2. Normalize leads           LeadNormalizer.normalize_many()
3. Accept Rinkel calls       (injected — no DB, no API)
4. Link both systems         AgentLinkingEngine.link()
5. Group data per agent      → AgentUnifiedProfile list
6. Compute metrics           MetricsCalculator.calculate_many()
7. Build audit reports       → AgentAuditReport list

ANSWERS THE QUESTION
---------------------
"How is each agent performing across CRM + calls?"

OUTPUT
------
AgentAuditReport (frozen dataclass):

  agent_id : str

  kommo : KommoSection
    total_leads      int
    converted_leads  int
    lost_leads       int
    active_leads     int
    conversion_rate  float (0.0 – 1.0)

  rinkel : RinkelSection
    total_calls       int
    avg_call_duration float   (seconds)
    inbound_calls     int
    outbound_calls    int
    engagement_score  float   (0.0 – 1.0)

  combined : CombinedSection
    performance_score        float (0.0 – 1.0)
    activity_consistency     float (0.0 – 1.0)
    leads_to_calls_ratio     float
    responsiveness_proxy     float (0.0 – 1.0)
    data_source_flags        dict  {kommo: bool, rinkel: bool}

PERFORMANCE SCORE FORMULA
--------------------------
performance_score = 0.40 × conversion_rate
                  + 0.35 × engagement_score
                  + 0.25 × activity_consistency

This rewards:
  - Converting leads (Kommo quality)
  - Being active in calls (Rinkel coverage)
  - Appearing in both systems (data completeness)

USAGE
-----
    from app.services.audit_engine import AuditEngine

    # Minimal — Kommo only
    engine = AuditEngine(exports_dir="exports/")
    reports = engine.run()

    for report in reports:
        print(report.agent_id, report.combined.performance_score)

    # With Rinkel calls
    engine = AuditEngine(
        exports_dir="exports/",
        rinkel_calls=rinkel_call_list,
        agent_id_map={"agent-nl-007": "10359915"},
    )
    reports = engine.run()

    # Single agent
    report = engine.run_for_agent("10359915")

    # As dicts (for JSONL / API)
    dicts = engine.run_as_dicts()

    # Summary table
    summary = engine.summary()
    # {
    #   "total_agents": 2,
    #   "total_leads": 448,
    #   "top_performer": "10359915",
    #   "avg_performance_score": 0.43,
    #   ...
    # }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.integrations.kommo import KommoProvider
from app.services.agent_linking_engine import AgentLinkingEngine
from app.services.lead_normalizer import LeadNormalizer, NormalizedLead
from app.services.metrics_calculator import AgentMetrics, MetricsCalculator

logger = logging.getLogger(__name__)


# ── Report model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KommoSection:
    """CRM performance section of the audit report."""
    total_leads:     int
    converted_leads: int
    lost_leads:      int
    active_leads:    int
    conversion_rate: float    # 0.0 – 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_leads":     self.total_leads,
            "converted_leads": self.converted_leads,
            "lost_leads":      self.lost_leads,
            "active_leads":    self.active_leads,
            "conversion_rate": self.conversion_rate,
        }


@dataclass(frozen=True)
class RinkelSection:
    """Call activity section of the audit report."""
    total_calls:       int
    avg_call_duration: float  # seconds
    inbound_calls:     int
    outbound_calls:    int
    engagement_score:  float  # 0.0 – 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_calls":       self.total_calls,
            "avg_call_duration": self.avg_call_duration,
            "inbound_calls":     self.inbound_calls,
            "outbound_calls":    self.outbound_calls,
            "engagement_score":  self.engagement_score,
        }


@dataclass(frozen=True)
class CombinedSection:
    """Cross-system derived metrics."""
    performance_score:    float  # 0.0 – 1.0  (primary KPI)
    activity_consistency: float  # 0.0 – 1.0
    leads_to_calls_ratio: float
    responsiveness_proxy: float  # 0.0 – 1.0
    data_source_flags:    dict[str, bool]  # {"kommo": True, "rinkel": False}

    def to_dict(self) -> dict[str, Any]:
        return {
            "performance_score":    self.performance_score,
            "activity_consistency": self.activity_consistency,
            "leads_to_calls_ratio": self.leads_to_calls_ratio,
            "responsiveness_proxy": self.responsiveness_proxy,
            "data_source_flags":    self.data_source_flags,
        }


@dataclass(frozen=True)
class AgentAuditReport:
    """
    Complete audit report for a single agent.

    Combines CRM, call, and cross-system data into a single read-only record.
    All float fields are rounded to 4 decimal places.
    """
    agent_id: str
    kommo:    KommoSection
    rinkel:   RinkelSection
    combined: CombinedSection

    # Denormalized leads for downstream use (not frozen — list of frozen objs)
    normalized_leads: tuple[NormalizedLead, ...]

    @property
    def performance_score(self) -> float:
        """Shortcut to combined.performance_score."""
        return self.combined.performance_score

    def to_dict(self) -> dict[str, Any]:
        """Return a flat, JSON-serialisable representation."""
        return {
            "agent_id": self.agent_id,
            "kommo":    self.kommo.to_dict(),
            "rinkel":   self.rinkel.to_dict(),
            "combined": self.combined.to_dict(),
        }

    def to_flat_dict(self) -> dict[str, Any]:
        """
        Return a single-level dict (useful for CSV / tabular outputs).

        Keys are prefixed: kommo_*, rinkel_*, combined_*.
        """
        flat: dict[str, Any] = {"agent_id": self.agent_id}
        for k, v in self.kommo.to_dict().items():
            flat[f"kommo_{k}"] = v
        for k, v in self.rinkel.to_dict().items():
            flat[f"rinkel_{k}"] = v
        for k, v in self.combined.to_dict().items():
            if k == "data_source_flags":
                flat["has_kommo_data"]  = v.get("kommo", False)
                flat["has_rinkel_data"] = v.get("rinkel", False)
            else:
                flat[f"combined_{k}"] = v
        return flat

    def __repr__(self) -> str:
        return (
            f"AgentAuditReport(agent_id={self.agent_id!r}, "
            f"leads={self.kommo.total_leads}, "
            f"calls={self.rinkel.total_calls}, "
            f"performance={self.combined.performance_score:.4f})"
        )


# ── Engine ────────────────────────────────────────────────────────────────────

class AuditEngine:
    """
    Full pipeline engine: Kommo + Rinkel → AgentAuditReport.

    Steps
    -----
    1. Fetch Kommo leads from disk via KommoProvider
    2. Normalize leads via LeadNormalizer
    3. Accept injected Rinkel calls (no DB/API calls)
    4. Link both systems via AgentLinkingEngine (join on responsible_user_id)
    5. Group data per agent → AgentUnifiedProfile list
    6. Compute metrics via MetricsCalculator
    7. Build AgentAuditReport per agent

    Parameters
    ----------
    exports_dir : str | None
        Path to Kommo JSON exports directory. Defaults to exports/.

    rinkel_calls : list[dict] | None
        Rinkel call records. Each must have an "agent_id" field.

    agent_id_map : dict[str, str] | None
        Explicit cross-reference {rinkel_agent_id → kommo_user_id}.
        Used when Rinkel IDs are not numeric copies of Kommo user IDs.

    include_kommo_only : bool
        Include agents that appear only in Kommo (no calls). Default True.

    include_rinkel_only : bool
        Include agents that appear only in Rinkel (no leads). Default True.
    """

    def __init__(
        self,
        exports_dir: str | None = None,
        rinkel_calls: list[dict[str, Any]] | None = None,
        agent_id_map: dict[str, str] | None = None,
        include_kommo_only: bool = True,
        include_rinkel_only: bool = True,
    ) -> None:
        self._exports_dir      = exports_dir
        self._rinkel_calls     = rinkel_calls or []
        self._agent_id_map     = agent_id_map or {}
        self._include_k_only   = include_kommo_only
        self._include_r_only   = include_rinkel_only

        # Services (instantiated once per engine)
        self._provider  = KommoProvider(exports_dir)
        stage_map       = self._provider.stages_by_id()
        self._normalizer  = LeadNormalizer(stage_map=stage_map)
        self._linker      = AgentLinkingEngine(agent_id_map=agent_id_map)
        self._calculator  = MetricsCalculator(stage_map=stage_map)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> list[AgentAuditReport]:
        """
        Execute the full 7-step pipeline and return all agent audit reports.

        Returns
        -------
        list[AgentAuditReport]
            Sorted by performance_score descending (top performers first).
            Empty list if no data is available.
        """
        # Step 1 — fetch Kommo leads
        raw_leads = self._provider.get_leads()
        logger.info("AuditEngine step 1: fetched Kommo leads", extra={"count": len(raw_leads)})

        # Step 2 — normalize leads
        normalized = self._normalizer.normalize_many(raw_leads)
        logger.info("AuditEngine step 2: normalized leads", extra={"count": len(normalized)})

        # Step 3 — Rinkel calls already held in memory
        logger.info("AuditEngine step 3: Rinkel calls ready", extra={"count": len(self._rinkel_calls)})

        # Step 4 — link both systems by agent ID
        profiles = self._linker.link(raw_leads, self._rinkel_calls)
        logger.info("AuditEngine step 4: linked agents", extra={"profiles": len(profiles)})

        # Step 5 — apply inclusion filters
        profiles = self._apply_filters(profiles)
        logger.info("AuditEngine step 5: filtered profiles", extra={"remaining": len(profiles)})

        # Step 6 — compute metrics per agent
        metrics_list = self._calculator.calculate_many(profiles)
        logger.info("AuditEngine step 6: metrics computed", extra={"count": len(metrics_list)})

        # Build a lookup: normalized leads by responsible_user_id (str)
        leads_by_agent = self._index_normalized_leads(normalized)

        # Step 7 — build reports
        reports = []
        for profile, metrics in zip(profiles, metrics_list):
            report = self._build_report(
                agent_id=profile.agent_id,
                metrics=metrics,
                norm_leads=leads_by_agent.get(profile.agent_id, []),
            )
            reports.append(report)

        # Sort: top performers first
        reports.sort(key=lambda r: r.performance_score, reverse=True)

        logger.info(
            "AuditEngine step 7: reports complete",
            extra={
                "total":   len(reports),
                "top":     reports[0].agent_id if reports else None,
                "bottom":  reports[-1].agent_id if reports else None,
            },
        )
        return reports

    def run_for_agent(self, agent_id: str) -> AgentAuditReport | None:
        """
        Run the full pipeline and return the report for a single agent.

        Returns None if the agent is not found.
        """
        reports = self.run()
        target = str(agent_id).strip()
        return next((r for r in reports if r.agent_id == target), None)

    def run_as_dicts(self) -> list[dict[str, Any]]:
        """
        Run the pipeline and return all reports as plain dicts.

        Suitable for JSONL export, REST API responses, and downstream tools.
        """
        return [r.to_dict() for r in self.run()]

    def run_as_flat_dicts(self) -> list[dict[str, Any]]:
        """
        Run the pipeline and return all reports as flat dicts.

        Suitable for CSV export and tabular processing.
        Each row has keys: agent_id, kommo_*, rinkel_*, combined_*.
        """
        return [r.to_flat_dict() for r in self.run()]

    def summary(self) -> dict[str, Any]:
        """
        Run the pipeline and return an aggregate summary dict.

        Returns
        -------
        dict with keys:
            total_agents          int
            total_leads           int
            total_calls           int
            matched_agents        int  (in both systems)
            kommo_only_agents     int
            rinkel_only_agents    int
            avg_performance_score float
            avg_conversion_rate   float
            avg_engagement_score  float
            top_performer         str | None  (agent_id with highest score)
            bottom_performer      str | None
        """
        reports = self.run()
        if not reports:
            return {
                "total_agents": 0,
                "total_leads": 0, "total_calls": 0,
                "matched_agents": 0, "kommo_only_agents": 0, "rinkel_only_agents": 0,
                "avg_performance_score": 0.0,
                "avg_conversion_rate": 0.0,
                "avg_engagement_score": 0.0,
                "top_performer": None,
                "bottom_performer": None,
            }

        n = len(reports)
        total_leads  = sum(r.kommo.total_leads  for r in reports)
        total_calls  = sum(r.rinkel.total_calls for r in reports)
        matched      = sum(1 for r in reports if r.combined.data_source_flags.get("kommo") and r.combined.data_source_flags.get("rinkel"))
        k_only       = sum(1 for r in reports if r.combined.data_source_flags.get("kommo") and not r.combined.data_source_flags.get("rinkel"))
        r_only       = sum(1 for r in reports if r.combined.data_source_flags.get("rinkel") and not r.combined.data_source_flags.get("kommo"))
        avg_perf     = round(sum(r.combined.performance_score  for r in reports) / n, 4)
        avg_conv     = round(sum(r.kommo.conversion_rate       for r in reports) / n, 4)
        avg_eng      = round(sum(r.rinkel.engagement_score     for r in reports) / n, 4)

        return {
            "total_agents":          n,
            "total_leads":           total_leads,
            "total_calls":           total_calls,
            "matched_agents":        matched,
            "kommo_only_agents":     k_only,
            "rinkel_only_agents":    r_only,
            "avg_performance_score": avg_perf,
            "avg_conversion_rate":   avg_conv,
            "avg_engagement_score":  avg_eng,
            "top_performer":         reports[0].agent_id,   # sorted desc by score
            "bottom_performer":      reports[-1].agent_id,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Report construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_report(
        self,
        agent_id: str,
        metrics: AgentMetrics,
        norm_leads: list[NormalizedLead],
    ) -> AgentAuditReport:
        """
        Construct an AgentAuditReport from an AgentMetrics object.

        Performance score formula:
            0.40 × conversion_rate      — CRM quality
          + 0.35 × engagement_score     — call activity vs leads
          + 0.25 × activity_consistency — present in both systems
        """
        performance_score = round(
            0.40 * metrics.conversion_rate
            + 0.35 * metrics.engagement_score
            + 0.25 * metrics.activity_consistency,
            4,
        )

        kommo = KommoSection(
            total_leads=     metrics.total_leads,
            converted_leads= metrics.converted_leads,
            lost_leads=      metrics.lost_leads,
            active_leads=    metrics.active_leads,
            conversion_rate= metrics.conversion_rate,
        )

        rinkel = RinkelSection(
            total_calls=       metrics.total_calls,
            avg_call_duration= metrics.avg_call_duration,
            inbound_calls=     metrics.inbound_calls,
            outbound_calls=    metrics.outbound_calls,
            engagement_score=  metrics.engagement_score,
        )

        combined = CombinedSection(
            performance_score=    performance_score,
            activity_consistency= metrics.activity_consistency,
            leads_to_calls_ratio= metrics.leads_to_calls_ratio,
            responsiveness_proxy= metrics.responsiveness_proxy,
            data_source_flags={
                "kommo":  metrics.total_leads > 0,
                "rinkel": metrics.total_calls > 0,
            },
        )

        return AgentAuditReport(
            agent_id=agent_id,
            kommo=kommo,
            rinkel=rinkel,
            combined=combined,
            normalized_leads=tuple(norm_leads),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_filters(self, profiles):
        filtered = []
        for p in profiles:
            if p.total_leads == 0 and p.total_calls == 0:
                continue  # nothing to report
            if not self._include_k_only and p.is_kommo_only:
                continue
            if not self._include_r_only and p.is_rinkel_only:
                continue
            filtered.append(p)
        return filtered

    @staticmethod
    def _index_normalized_leads(
        normalized: list[NormalizedLead],
    ) -> dict[str, list[NormalizedLead]]:
        """
        Build index: agent_id (str) → [NormalizedLead, ...].

        Uses NormalizedLead.responsible_user_id as the key.
        """
        index: dict[str, list[NormalizedLead]] = {}
        for lead in normalized:
            uid = lead.responsible_user_id
            if uid:
                index.setdefault(uid, []).append(lead)
        return index

    def __repr__(self) -> str:
        files = self._provider.available_files()
        present = [k for k, v in files.items() if v]
        return (
            f"AuditEngine("
            f"exports={present}, "
            f"rinkel_calls={len(self._rinkel_calls)})"
        )
