"""
KommoAuditService — unified read-only data service for Kommo CRM audit workflows.

PURPOSE
-------
Single high-level entry point that wires together:

    KommoProvider       — reads raw JSON exports from disk
    LeadNormalizer      — converts raw leads → stable NormalizedLead objects
    AgentLinkingEngine  — unifies Kommo agents with Rinkel call records

Consumers (Claude audit prompts, JSONL exporters, analytics) call this
service and receive clean, typed, audit-ready data without touching
any of the underlying providers directly.

DESIGN PRINCIPLES
-----------------
- Read-only and stateless (no mutations, no DB, no API calls).
- Lazy loading — files are only read when first requested; results cached.
- Deterministic outputs — same files always produce the same data.
- Fault-tolerant — every method returns a safe default on failure.

USAGE
-----
    from app.services.kommo_audit_service import KommoAuditService

    service = KommoAuditService()

    # Normalised leads
    leads = service.normalized_leads()           # list[NormalizedLead]

    # Unified agent profiles (Kommo + Rinkel joined)
    profiles = service.agent_profiles()          # list[AgentUnifiedProfile]
    profile  = service.agent_profile("10359915") # AgentUnifiedProfile | None

    # Summary stats
    stats = service.summary()
    # {
    #   "total_leads": 448,
    #   "total_agents": 2,
    #   "matched_agents": 2,
    #   "kommo_only_agents": 0,
    #   "rinkel_only_agents": 0,
    #   "total_pipelines": 11,
    #   "total_stages": 90,
    #   "kommo_extracted_at": "2026-05-28T06:00:09Z",
    # }

    # Raw access (pass-through to KommoProvider)
    raw_leads     = service.raw_leads()
    raw_pipelines = service.raw_pipelines()
    raw_chats     = service.raw_chats()
    raw_messages  = service.raw_messages()

    # Stage enrichment
    stage = service.resolve_stage("62386811")    # {"stage_name": "Incoming leads", ...}

    # Audit-ready agent context dict (for Claude prompts / JSONL)
    context = service.agent_audit_context("10359915")
"""

from __future__ import annotations

import logging
from typing import Any

from app.integrations.kommo import KommoProvider
from app.services.agent_linking_engine import AgentLinkingEngine, AgentUnifiedProfile
from app.services.lead_normalizer import LeadNormalizer, NormalizedLead

logger = logging.getLogger(__name__)

# Sentinel — used to distinguish "not yet loaded" from None
_UNSET = object()


class KommoAuditService:
    """
    High-level read-only service for Kommo CRM audit data.

    Wires KommoProvider → LeadNormalizer → AgentLinkingEngine into a single
    clean API.  All results are cached after first access.

    Parameters
    ----------
    exports_dir : str | None
        Path to the directory containing Kommo JSON exports.
        Defaults to <project_root>/exports/.

    rinkel_calls : list[dict] | None
        Optional Rinkel call records to link against Kommo agents.
        When provided, agent profiles will include matched calls.
        When None, agent profiles are Kommo-only.

    agent_id_map : dict[str, str] | None
        Optional cross-reference {rinkel_agent_id → kommo_user_id}.
        Passed through to AgentLinkingEngine.
    """

    def __init__(
        self,
        exports_dir: str | None = None,
        rinkel_calls: list[dict[str, Any]] | None = None,
        agent_id_map: dict[str, str] | None = None,
    ) -> None:
        self._provider = KommoProvider(exports_dir)
        self._rinkel_calls: list[dict[str, Any]] = rinkel_calls or []
        self._agent_id_map: dict[str, str] = agent_id_map or {}

        # Lazy caches
        self._cache_normalized_leads: list[NormalizedLead] | object = _UNSET
        self._cache_profiles: list[AgentUnifiedProfile] | object = _UNSET
        self._cache_profile_map: dict[str, AgentUnifiedProfile] | object = _UNSET
        self._cache_stages: dict[str, dict[str, Any]] | object = _UNSET
        self._cache_summary: dict[str, Any] | object = _UNSET

    # ─────────────────────────────────────────────────────────────────────────
    # Raw pass-through accessors (direct from KommoProvider)
    # ─────────────────────────────────────────────────────────────────────────

    def raw_leads(self) -> list[dict[str, Any]]:
        """Return raw Kommo lead dicts (unmodified)."""
        return self._provider.get_leads()

    def raw_pipelines(self) -> list[dict[str, Any]]:
        """Return raw Kommo pipeline dicts (unmodified)."""
        return self._provider.get_pipelines()

    def raw_chats(self) -> list[dict[str, Any]]:
        """Return raw Kommo chat records (unmodified)."""
        return self._provider.get_chats()

    def raw_messages(self) -> list[dict[str, Any]]:
        """Return raw Kommo message records (unmodified)."""
        return self._provider.get_messages()

    def available_files(self) -> dict[str, bool]:
        """Return a map of which export files exist on disk."""
        return self._provider.available_files()

    # ─────────────────────────────────────────────────────────────────────────
    # Normalised leads
    # ─────────────────────────────────────────────────────────────────────────

    def normalized_leads(self) -> list[NormalizedLead]:
        """
        Return all Kommo leads normalised to stable NormalizedLead objects.

        Status fields are resolved to human-readable stage names when
        pipeline data is available.

        Returns [] if leads.json is absent or malformed.
        Cached after first call.
        """
        if self._cache_normalized_leads is _UNSET:
            stage_map = self._provider.stages_by_id()
            normalizer = LeadNormalizer(stage_map=stage_map)
            self._cache_normalized_leads = normalizer.normalize_many(
                self._provider.get_leads()
            )
            logger.debug(
                "KommoAuditService: normalised leads loaded",
                extra={"count": len(self._cache_normalized_leads)},  # type: ignore[arg-type]
            )
        return self._cache_normalized_leads  # type: ignore[return-value]

    # ─────────────────────────────────────────────────────────────────────────
    # Agent profiles
    # ─────────────────────────────────────────────────────────────────────────

    def agent_profiles(self) -> list[AgentUnifiedProfile]:
        """
        Return all unified agent profiles (Kommo leads + Rinkel calls merged).

        Output is sorted by agent_id for determinism.
        Cached after first call.
        """
        if self._cache_profiles is _UNSET:
            engine = AgentLinkingEngine(agent_id_map=self._agent_id_map)
            self._cache_profiles = engine.link(
                self._provider.get_leads(),
                self._rinkel_calls,
            )
            logger.debug(
                "KommoAuditService: agent profiles built",
                extra={"count": len(self._cache_profiles)},  # type: ignore[arg-type]
            )
        return self._cache_profiles  # type: ignore[return-value]

    def agent_profile(self, agent_id: str) -> AgentUnifiedProfile | None:
        """
        Return the profile for a single agent by ID, or None if not found.

        Args:
            agent_id: Kommo responsible_user_id as a string, or Rinkel agent_id.

        Returns:
            AgentUnifiedProfile or None.
        """
        if self._cache_profile_map is _UNSET:
            self._cache_profile_map = {
                p.agent_id: p for p in self.agent_profiles()
            }
        return self._cache_profile_map.get(str(agent_id).strip())  # type: ignore[union-attr]

    def agent_ids(self) -> list[str]:
        """Return all agent IDs present in the unified profile list."""
        return [p.agent_id for p in self.agent_profiles()]

    # ─────────────────────────────────────────────────────────────────────────
    # Stage enrichment
    # ─────────────────────────────────────────────────────────────────────────

    def resolve_stage(self, stage_id: str | int) -> dict[str, Any] | None:
        """
        Resolve a stage_id to its full stage dict.

        Args:
            stage_id: int or str representation of the stage ID.

        Returns:
            Stage dict with keys: stage_id, stage_name, pipeline_id, sort, color
            or None if not found.
        """
        if self._cache_stages is _UNSET:
            self._cache_stages = {
                str(k): v
                for k, v in self._provider.stages_by_id().items()
            }
        try:
            key = str(int(stage_id))
        except (TypeError, ValueError):
            return None
        return self._cache_stages.get(key)  # type: ignore[union-attr]

    # ─────────────────────────────────────────────────────────────────────────
    # Audit context (Claude-ready dicts)
    # ─────────────────────────────────────────────────────────────────────────

    def agent_audit_context(self, agent_id: str) -> dict[str, Any] | None:
        """
        Build a Claude/JSONL-ready audit context dict for a single agent.

        The returned dict contains all data needed for a QA audit prompt:
            agent_id:         str
            lead_count:       int
            call_count:       int
            is_matched:       bool
            normalized_leads: list[dict]   — NormalizedLead.to_dict() for each lead
            rinkel_calls:     list[dict]   — raw Rinkel call dicts
            pipeline_summary: dict         — unique pipelines/stages the agent works in

        Returns None if agent_id is not found.
        """
        profile = self.agent_profile(agent_id)
        if profile is None:
            return None

        # Normalise the agent's specific leads
        stage_map = self._provider.stages_by_id()
        normalizer = LeadNormalizer(stage_map=stage_map)
        norm_leads = [
            normalizer.normalize(lead).to_dict()
            for lead in profile.kommo_leads
        ]

        # Build pipeline summary
        pipeline_map = self._provider.pipelines_by_id()
        pipeline_ids = {
            lead.get("pipeline_id")
            for lead in profile.kommo_leads
            if isinstance(lead.get("pipeline_id"), int)
        }
        pipelines_used = []
        for pid in sorted(pipeline_ids):
            p = pipeline_map.get(pid)
            if p:
                pipelines_used.append({
                    "pipeline_id": pid,
                    "pipeline_name": p.get("pipeline_name"),
                    "total_stages": p.get("total_stages"),
                })

        return {
            "agent_id": profile.agent_id,
            "lead_count": profile.total_leads,
            "call_count": profile.total_calls,
            "is_matched": profile.is_matched,
            "normalized_leads": norm_leads,
            "rinkel_calls": profile.rinkel_calls,
            "pipeline_summary": pipelines_used,
        }

    def all_audit_contexts(self) -> list[dict[str, Any]]:
        """
        Return audit context dicts for ALL agents.

        Useful for batch JSONL export or bulk Claude processing.
        Agents with zero leads AND zero calls are excluded.
        """
        contexts = []
        for profile in self.agent_profiles():
            if profile.total_leads + profile.total_calls == 0:
                continue
            ctx = self.agent_audit_context(profile.agent_id)
            if ctx is not None:
                contexts.append(ctx)
        return contexts

    # ─────────────────────────────────────────────────────────────────────────
    # Summary stats
    # ─────────────────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """
        Return a high-level summary of the loaded Kommo data.

        Useful for health checks, dashboards, and audit reports.

        Returns
        -------
        dict with keys:
            total_leads        int
            total_agents       int
            matched_agents     int  — in both Kommo and Rinkel
            kommo_only_agents  int
            rinkel_only_agents int
            total_pipelines    int
            total_stages       int
            total_chats        int
            total_messages     int
            kommo_extracted_at str | None
            files_available    dict[str, bool]
        """
        if self._cache_summary is _UNSET:
            profiles = self.agent_profiles()
            leads_meta = self._provider.meta("leads")
            self._cache_summary = {
                "total_leads":        len(self.raw_leads()),
                "total_agents":       len(profiles),
                "matched_agents":     sum(1 for p in profiles if p.is_matched),
                "kommo_only_agents":  sum(1 for p in profiles if p.is_kommo_only),
                "rinkel_only_agents": sum(1 for p in profiles if p.is_rinkel_only),
                "total_pipelines":    len(self.raw_pipelines()),
                "total_stages":       len(self._provider.stages_by_id()),
                "total_chats":        len(self.raw_chats()),
                "total_messages":     len(self.raw_messages()),
                "kommo_extracted_at": leads_meta.get("extracted_at"),
                "files_available":    self.available_files(),
            }
        return self._cache_summary  # type: ignore[return-value]

    # ─────────────────────────────────────────────────────────────────────────
    # Cache management
    # ─────────────────────────────────────────────────────────────────────────

    def clear_cache(self) -> None:
        """
        Reset all internal caches.

        Call this if the export files on disk have been updated and you need
        a fresh read without creating a new service instance.
        """
        self._cache_normalized_leads = _UNSET
        self._cache_profiles = _UNSET
        self._cache_profile_map = _UNSET
        self._cache_stages = _UNSET
        self._cache_summary = _UNSET
        self._provider._cache.clear()  # also clear provider's file cache
        logger.debug("KommoAuditService: all caches cleared")

    def __repr__(self) -> str:
        files = self._provider.available_files()
        present = [k for k, v in files.items() if v]
        return (
            f"KommoAuditService("
            f"exports_dir={str(self._provider.exports_dir())!r}, "
            f"files={present}, "
            f"rinkel_calls={len(self._rinkel_calls)})"
        )
