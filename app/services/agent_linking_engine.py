"""
AgentLinkingEngine — unifies Kommo CRM leads and Rinkel call records
under a single agent identity.

PURPOSE
-------
Both systems use different agent identifiers:

    Kommo:  responsible_user_id  (int, e.g. 10359915)
    Rinkel: agent_id             (str, e.g. "agent-nl-007" or "10359915")

The engine accepts an optional `agent_id_map` that cross-references these
systems.  Without the map it still works: agents present in only one
system are included, never dropped.

DESIGN PRINCIPLES
-----------------
- Pure function — no I/O, no mutations, no side effects.
- Deterministic — identical inputs always produce identical outputs.
- Non-destructive — original dicts are never modified.
- Complete — unmatched records are NEVER dropped (one-sided profiles allowed).
- Type-safe — all outputs use typed dataclasses.
- Fault-tolerant — missing/null agent keys are handled gracefully.

INPUT
-----
kommo_leads: list[dict]
    Raw lead dicts from KommoProvider.get_leads().
    Join key: lead["responsible_user_id"] (int)

rinkel_calls: list[dict]
    Raw call dicts from the Rinkel webhook pipeline.
    The engine recognises agent_id from both:
      - webhook payloads:  call["agent_id"]  (str)
      - DB-stored records: call["agent_id"]  (str | UUID str)

agent_id_map: dict[str, str] | None
    Optional explicit cross-reference: {rinkel_agent_id → kommo_user_id}.
    When provided, the engine uses it to merge agents across both systems.
    When absent, the engine auto-matches where both sides use the same
    numeric string (e.g. Rinkel agent_id "10359915" == Kommo user 10359915).

OUTPUT
------
list[AgentUnifiedProfile]

    AgentUnifiedProfile:
        agent_id:     str              — canonical agent identifier
        kommo_leads:  list[dict]       — raw Kommo lead dicts (never mutated)
        rinkel_calls: list[dict]       — raw Rinkel call dicts (never mutated)

    The profile list is sorted by agent_id for determinism.
    Agents with zero kommo_leads OR zero rinkel_calls are still included.

USAGE
-----
    from app.services.agent_linking_engine import AgentLinkingEngine
    from app.integrations.kommo import KommoProvider

    provider   = KommoProvider()
    kommo_leads = provider.get_leads()

    # rinkel_calls can be any list of dicts with an "agent_id" field
    rinkel_calls = [...]

    engine   = AgentLinkingEngine()
    profiles = engine.link(kommo_leads, rinkel_calls)

    for profile in profiles:
        print(profile.agent_id, len(profile.kommo_leads), len(profile.rinkel_calls))

    # With an explicit cross-reference map:
    engine = AgentLinkingEngine(
        agent_id_map={"agent-nl-007": "10359915"}
    )
    profiles = engine.link(kommo_leads, rinkel_calls)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass
class AgentUnifiedProfile:
    """
    Unified agent view merging Kommo CRM data and Rinkel call records.

    Fields
    ------
    agent_id : str
        Canonical agent identifier used as the join key.
        For Kommo-only agents: str(responsible_user_id).
        For Rinkel-only agents: the raw agent_id string.
        For matched agents: the Kommo user ID string (preferred as canonical).

    kommo_leads : list[dict]
        Raw lead dicts from KommoProvider — never mutated, never copied.

    rinkel_calls : list[dict]
        Raw call dicts from the Rinkel pipeline — never mutated, never copied.

    Properties
    ----------
    is_matched : bool
        True when the agent appears in BOTH systems.
    is_kommo_only : bool
        True when the agent has leads but no calls.
    is_rinkel_only : bool
        True when the agent has calls but no leads.
    total_calls : int
    total_leads : int
    """

    agent_id: str
    kommo_leads: list[dict[str, Any]] = field(default_factory=list)
    rinkel_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_matched(self) -> bool:
        """Agent appears in both Kommo and Rinkel."""
        return bool(self.kommo_leads) and bool(self.rinkel_calls)

    @property
    def is_kommo_only(self) -> bool:
        """Agent exists in Kommo but has no Rinkel calls."""
        return bool(self.kommo_leads) and not self.rinkel_calls

    @property
    def is_rinkel_only(self) -> bool:
        """Agent exists in Rinkel but has no Kommo leads."""
        return bool(self.rinkel_calls) and not self.kommo_leads

    @property
    def total_calls(self) -> int:
        return len(self.rinkel_calls)

    @property
    def total_leads(self) -> int:
        return len(self.kommo_leads)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable representation (lead/call dicts are NOT copied)."""
        return {
            "agent_id":     self.agent_id,
            "kommo_leads":  self.kommo_leads,
            "rinkel_calls": self.rinkel_calls,
            "is_matched":   self.is_matched,
            "total_leads":  self.total_leads,
            "total_calls":  self.total_calls,
        }

    def __repr__(self) -> str:
        return (
            f"AgentUnifiedProfile(agent_id={self.agent_id!r}, "
            f"leads={self.total_leads}, calls={self.total_calls})"
        )


# ── Engine ────────────────────────────────────────────────────────────────────

class AgentLinkingEngine:
    """
    Links Kommo CRM leads and Rinkel call records under unified agent profiles.

    Parameters
    ----------
    agent_id_map : dict[str, str] | None
        Explicit cross-reference: {rinkel_agent_id → kommo_responsible_user_id}.

        Example:
            {"agent-nl-007": "10359915", "sophie.vd": "10606743"}

        When omitted, the engine auto-matches agents whose rinkel agent_id
        is a numeric string equal to their Kommo responsible_user_id.
        This works for systems where Rinkel is configured with Kommo user IDs
        as agent identifiers.
    """

    def __init__(
        self,
        agent_id_map: dict[str, str] | None = None,
    ) -> None:
        # Normalise: all keys and values are stripped strings
        self._agent_id_map: dict[str, str] = {
            str(k).strip(): str(v).strip()
            for k, v in (agent_id_map or {}).items()
            if k is not None and v is not None
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def link(
        self,
        kommo_leads: list[dict[str, Any]],
        rinkel_calls: list[dict[str, Any]],
    ) -> list[AgentUnifiedProfile]:
        """
        Produce a unified list of AgentUnifiedProfile objects.

        Algorithm
        ---------
        1. Index Kommo leads by responsible_user_id (str normalised).
        2. Index Rinkel calls by agent_id (str normalised).
        3. Build the reverse map: kommo_user_id → rinkel_agent_id
           (from agent_id_map + auto-matching of numeric IDs).
        4. Merge: for each Kommo agent, collect matching Rinkel calls.
        5. Add Rinkel-only agents (calls with no Kommo match).
        6. Sort profiles by agent_id for determinism.

        Parameters
        ----------
        kommo_leads : list[dict]
            Raw lead dicts from KommoProvider.  Unmodified.
        rinkel_calls : list[dict]
            Raw call dicts from the Rinkel webhook/DB pipeline.  Unmodified.

        Returns
        -------
        list[AgentUnifiedProfile]
            Sorted by agent_id ascending.  Never empty (returns [] only when
            both inputs are empty or contain no identifiable agents).
        """
        if not isinstance(kommo_leads, list):
            logger.warning(
                "AgentLinkingEngine.link: kommo_leads is not a list",
                extra={"type": type(kommo_leads).__name__},
            )
            kommo_leads = []

        if not isinstance(rinkel_calls, list):
            logger.warning(
                "AgentLinkingEngine.link: rinkel_calls is not a list",
                extra={"type": type(rinkel_calls).__name__},
            )
            rinkel_calls = []

        # Step 1 — index Kommo leads by responsible_user_id
        kommo_index: dict[str, list[dict]] = self._index_kommo(kommo_leads)

        # Step 2 — index Rinkel calls by agent_id
        rinkel_index: dict[str, list[dict]] = self._index_rinkel(rinkel_calls)

        # Step 3 — build kommo_user_id → rinkel_agent_id resolution map
        resolution = self._build_resolution(
            kommo_keys=set(kommo_index.keys()),
            rinkel_keys=set(rinkel_index.keys()),
        )
        # resolution: {kommo_user_id → rinkel_agent_id}

        # Step 4 — build profiles for all Kommo agents
        profiles: dict[str, AgentUnifiedProfile] = {}

        for kommo_uid, leads in kommo_index.items():
            rinkel_aid = resolution.get(kommo_uid)  # may be None
            calls = rinkel_index.get(rinkel_aid, []) if rinkel_aid else []

            profiles[kommo_uid] = AgentUnifiedProfile(
                agent_id=kommo_uid,
                kommo_leads=leads,
                rinkel_calls=calls,
            )

        # Step 5 — add Rinkel-only agents (no Kommo match)
        matched_rinkel_ids = set(resolution.values())
        for rinkel_aid, calls in rinkel_index.items():
            if rinkel_aid not in matched_rinkel_ids:
                # No Kommo counterpart — create Rinkel-only profile
                profiles[rinkel_aid] = AgentUnifiedProfile(
                    agent_id=rinkel_aid,
                    kommo_leads=[],
                    rinkel_calls=calls,
                )

        # Step 6 — sort deterministically
        result = sorted(profiles.values(), key=lambda p: p.agent_id)

        logger.info(
            "AgentLinkingEngine.link complete",
            extra={
                "total_profiles":  len(result),
                "matched":         sum(1 for p in result if p.is_matched),
                "kommo_only":      sum(1 for p in result if p.is_kommo_only),
                "rinkel_only":     sum(1 for p in result if p.is_rinkel_only),
                "kommo_leads_in":  len(kommo_leads),
                "rinkel_calls_in": len(rinkel_calls),
            },
        )
        return result

    def link_as_dict(
        self,
        kommo_leads: list[dict[str, Any]],
        rinkel_calls: list[dict[str, Any]],
    ) -> dict[str, AgentUnifiedProfile]:
        """
        Like link(), but returns a dict keyed by agent_id for O(1) lookup.

        Returns
        -------
        {agent_id: AgentUnifiedProfile}
        """
        return {p.agent_id: p for p in self.link(kommo_leads, rinkel_calls)}

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_kommo_user_id(lead: Any) -> str | None:
        """
        Extract and normalise the responsible_user_id from a raw Kommo lead.

        Returns str representation, or None if missing/null/invalid.
        Booleans are intentionally rejected (False == 0 in Python).
        """
        if not isinstance(lead, dict):
            return None
        uid = lead.get("responsible_user_id")
        if uid is None or isinstance(uid, bool):
            return None
        if isinstance(uid, (int, float)):
            return str(int(uid))
        if isinstance(uid, str):
            stripped = uid.strip()
            return stripped if stripped else None
        return None

    @staticmethod
    def _extract_rinkel_agent_id(call: Any) -> str | None:
        """
        Extract and normalise the agent_id from a raw Rinkel call dict.

        Handles:
          - Webhook payloads:  call["agent_id"]
          - DB records:        call["agent_id"] (may be UUID str or Kommo ID)
          - Nested payloads:   call["data"]["agent_id"]

        Returns str, or None if missing/null/empty.
        """
        if not isinstance(call, dict):
            return None

        # Direct key (most common — webhook + DB records)
        aid = call.get("agent_id")
        if aid is None:
            # Try nested under 'data' (some webhook formats)
            data = call.get("data")
            if isinstance(data, dict):
                aid = data.get("agent_id")

        if aid is None or isinstance(aid, bool):
            return None
        stripped = str(aid).strip()
        return stripped if stripped else None

    def _index_kommo(
        self, leads: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Build index: kommo_user_id → [lead, lead, ...].

        Leads with no extractable responsible_user_id are logged and skipped.
        """
        index: dict[str, list[dict]] = {}
        skipped = 0
        for lead in leads:
            uid = self._extract_kommo_user_id(lead)
            if uid is None:
                skipped += 1
                continue
            index.setdefault(uid, []).append(lead)
        if skipped:
            logger.debug(
                "Skipped Kommo leads with no responsible_user_id",
                extra={"count": skipped},
            )
        return index

    def _index_rinkel(
        self, calls: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Build index: rinkel_agent_id → [call, call, ...].

        Calls with no extractable agent_id are accumulated under a special
        _UNIDENTIFIED key so they appear in the output and are never dropped.
        """
        index: dict[str, list[dict]] = {}
        unidentified = 0
        for call in calls:
            aid = self._extract_rinkel_agent_id(call)
            if aid is None:
                unidentified += 1
                aid = "__unidentified__"
            index.setdefault(aid, []).append(call)
        if unidentified:
            logger.debug(
                "Rinkel calls with no agent_id placed in __unidentified__",
                extra={"count": unidentified},
            )
        return index

    def _build_resolution(
        self,
        kommo_keys: set[str],
        rinkel_keys: set[str],
    ) -> dict[str, str]:
        """
        Build the kommo_user_id → rinkel_agent_id resolution map.

        Priority
        --------
        1. Explicit agent_id_map entries (highest priority).
        2. Auto-match: when a rinkel_agent_id is numeric and matches
           a kommo_user_id exactly (both normalised to str).

        Returns
        -------
        dict: {kommo_user_id → rinkel_agent_id}
        """
        # Invert agent_id_map: {rinkel_id → kommo_id}
        # → we need: {kommo_id → rinkel_id}
        explicit: dict[str, str] = {}
        for rinkel_id, kommo_id in self._agent_id_map.items():
            # Resolve: kommo_id from map → this rinkel_id
            if kommo_id in kommo_keys:
                explicit[kommo_id] = rinkel_id

        # Auto-match numeric rinkel IDs to kommo IDs
        auto: dict[str, str] = {}
        for rinkel_id in rinkel_keys:
            if rinkel_id.lstrip("-").isdigit():
                # Numeric rinkel agent_id → try matching to same kommo user_id
                if rinkel_id in kommo_keys and rinkel_id not in explicit:
                    auto[rinkel_id] = rinkel_id

        # Explicit overrides auto
        resolution = {**auto, **explicit}

        logger.debug(
            "Agent resolution map built",
            extra={
                "explicit_matches": len(explicit),
                "auto_matches":     len(auto),
                "total_kommo":      len(kommo_keys),
                "total_rinkel":     len(rinkel_keys),
            },
        )
        return resolution
