"""
LeadNormalizer — converts raw Kommo leads into a stable internal format.

PURPOSE
-------
Takes raw lead dicts from KommoProvider and returns a consistent
NormalizedLead object suitable for analytics, auditing, and downstream joins.

DESIGN CONTRACT
---------------
- NEVER raises — every field falls back to None on any failure.
- NEVER mutates the input dict.
- All output fields are str | None — callers never need to coerce types.
- Deterministic: same input always produces identical output.
- Status resolution is optional (pass a stage map to enrich status).

INPUT
-----
Raw lead dict from KommoProvider.get_leads().  Real Kommo v4 shape:

    {
        "id":                  25892146,       # int, always present in real data
        "name":                "...",           # str, always present in real data
        "pipeline_id":         11231784,        # int, always present
        "status_id":           143,             # int, always present
        "responsible_user_id": 10359915,        # int, always present
        "created_at":          1779623803,      # int (unix), always present
        "updated_at":          1779812622,      # int (unix), always present
        "created_at_iso":      "2026-05-24T11:56:43+00:00",  # str, present in v4 exports
        "updated_at_iso":      "2026-05-26T16:23:42+00:00",  # str, present in v4 exports
        "closed_at":           1779812620,      # int | None
        "is_deleted":          False,           # bool
        "score":               None,
        ...
    }

OUTPUT (NormalizedLead dataclass)
----------------------------------
    id:                  str | None   — always str("int_value") or None
    name:                str | None
    pipeline_id:         str | None   — always str("int_value") or None
    responsible_user_id: str | None   — always str("int_value") or None
    created_at:          str | None   — ISO 8601 string preferred; unix str fallback
    updated_at:          str | None   — ISO 8601 string preferred; unix str fallback
    status:              str | None   — stage name if stage_map provided, else str(status_id)

USAGE
-----
    from app.services.lead_normalizer import LeadNormalizer, NormalizedLead
    from app.integrations.kommo import KommoProvider

    provider = KommoProvider()
    normalizer = LeadNormalizer()
    leads = [normalizer.normalize(raw) for raw in provider.get_leads()]

    # With pipeline stage resolution (recommended):
    stage_map = provider.stages_by_id()
    normalizer = LeadNormalizer(stage_map=stage_map)
    leads = normalizer.normalize_many(provider.get_leads())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedLead:
    """
    Stable internal representation of a Kommo lead.

    All fields are str | None.  No integers, no booleans, no dicts.
    Immutable (frozen dataclass) — safe to use as dict keys or in sets.
    """

    id: str | None
    name: str | None
    pipeline_id: str | None
    responsible_user_id: str | None
    created_at: str | None
    updated_at: str | None
    status: str | None

    def to_dict(self) -> dict[str, str | None]:
        """Return a plain dict representation."""
        return {
            "id": self.id,
            "name": self.name,
            "pipeline_id": self.pipeline_id,
            "responsible_user_id": self.responsible_user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
        }


class LeadNormalizer:
    """
    Converts raw Kommo lead dicts into NormalizedLead instances.

    Args:
        stage_map: Optional dict mapping stage_id (int) → stage dict.
                   When provided, the `status` field is resolved to the
                   human-readable stage name.
                   Obtain via: KommoProvider().stages_by_id()

    Example:
        normalizer = LeadNormalizer(stage_map=provider.stages_by_id())
        lead = normalizer.normalize(raw_lead)
        print(lead.status)  # "Frios" instead of "85775856"
    """

    def __init__(
        self,
        stage_map: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        # Normalise to empty dict so all lookups are safe
        self._stage_map: dict[int, dict[str, Any]] = stage_map or {}

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def normalize(self, raw: Any) -> NormalizedLead:
        """
        Convert a single raw Kommo lead dict into a NormalizedLead.

        Never raises — any unexpected input returns a NormalizedLead with
        all fields set to None.

        Args:
            raw: A dict (or any value) from KommoProvider.get_leads().

        Returns:
            NormalizedLead with all fields as str | None.
        """
        if not isinstance(raw, dict):
            logger.warning(
                "LeadNormalizer.normalize received non-dict input",
                extra={"type": type(raw).__name__, "value": repr(raw)[:80]},
            )
            return _NULL_LEAD

        try:
            return NormalizedLead(
                id=self._to_str(raw.get("id")),
                name=self._to_str_or_none(raw.get("name")),
                pipeline_id=self._to_str(raw.get("pipeline_id")),
                responsible_user_id=self._to_str(raw.get("responsible_user_id")),
                created_at=self._resolve_timestamp(
                    raw.get("created_at_iso"),
                    raw.get("created_at"),
                ),
                updated_at=self._resolve_timestamp(
                    raw.get("updated_at_iso"),
                    raw.get("updated_at"),
                ),
                status=self._resolve_status(raw.get("status_id")),
            )
        except Exception as exc:  # noqa: BLE001 — total safety net
            logger.error(
                "LeadNormalizer.normalize failed unexpectedly",
                extra={"error": str(exc), "lead_id": repr(raw.get("id"))[:40]},
                exc_info=True,
            )
            return _NULL_LEAD

    def normalize_many(self, raws: Any) -> list[NormalizedLead]:
        """
        Normalize a list of raw lead dicts.

        Skips any record that fails without interrupting the batch.

        Args:
            raws: A list of raw lead dicts (or any iterable).

        Returns:
            List of NormalizedLead.  Length may be shorter than input if
            non-dict records are encountered.
        """
        if not hasattr(raws, "__iter__"):
            logger.warning(
                "LeadNormalizer.normalize_many received non-iterable",
                extra={"type": type(raws).__name__},
            )
            return []

        results = []
        for raw in raws:
            results.append(self.normalize(raw))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers — each is guaranteed never to raise
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_str(value: Any) -> str | None:
        """
        Convert any scalar to its string representation.

        Returns None if value is None, empty string, or not safely stringifiable.
        Booleans are NOT treated as ints (False → None, True → None) to
        avoid accidental truthy ID conversions.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            # Booleans pass isinstance(bool_val, int) in Python — guard first
            return None
        if isinstance(value, (int, float, str)):
            s = str(value).strip()
            return s if s else None
        # Unknown type — log and skip
        logger.debug(
            "LeadNormalizer._to_str: unexpected type",
            extra={"type": type(value).__name__, "value": repr(value)[:60]},
        )
        return None

    @staticmethod
    def _to_str_or_none(value: Any) -> str | None:
        """
        Like _to_str but also returns None for whitespace-only strings.
        Used for human-readable fields like `name`.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        if isinstance(value, (int, float)):
            return str(value).strip() or None
        return None

    @staticmethod
    def _resolve_timestamp(iso_value: Any, unix_value: Any) -> str | None:
        """
        Prefer ISO 8601 string; fall back to stringified unix timestamp.

        Real Kommo v4 exports include both:
            "created_at_iso": "2026-05-24T11:56:43+00:00"
            "created_at":     1779623803

        Strategy:
            1. Use iso_value if it is a non-empty string.
            2. Fall back to str(unix_value) if it is a non-negative int/float.
            3. Return None.
        """
        # Prefer ISO string
        if isinstance(iso_value, str):
            stripped = iso_value.strip()
            if stripped:
                return stripped

        # Fall back to unix timestamp
        if isinstance(unix_value, bool):
            return None
        if isinstance(unix_value, (int, float)) and unix_value >= 0:
            return str(int(unix_value))

        return None

    def _resolve_status(self, status_id: Any) -> str | None:
        """
        Map status_id to a human-readable stage name.

        If a stage_map was provided at construction and contains the status_id,
        returns the stage_name from that map.  Otherwise returns str(status_id).

        Returns None if status_id is None or not a valid int.
        """
        if status_id is None or isinstance(status_id, bool):
            return None

        if not isinstance(status_id, int):
            # Try coercing floats (e.g. 143.0 from some export tools)
            try:
                status_id = int(status_id)
            except (TypeError, ValueError):
                return None

        # Attempt stage name lookup
        if self._stage_map:
            stage = self._stage_map.get(status_id)
            if stage and isinstance(stage, dict):
                stage_name = stage.get("stage_name")
                if isinstance(stage_name, str) and stage_name.strip():
                    return stage_name.strip()

        # Fall back to raw status_id as string
        return str(status_id)


# ── Singleton null lead ────────────────────────────────────────────────────────
# Returned for any completely unrecoverable input — avoids allocating a new
# object on every bad record.
_NULL_LEAD = NormalizedLead(
    id=None,
    name=None,
    pipeline_id=None,
    responsible_user_id=None,
    created_at=None,
    updated_at=None,
    status=None,
)
