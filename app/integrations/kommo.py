"""
KommoProvider — read-only data provider for Kommo CRM local JSON exports.

PURPOSE
-------
Reads pre-exported Kommo JSON files from the local `exports/` directory.
This is a pure file-system provider: it makes no API calls, touches no
database, and performs no ingestion.  It is intentionally read-only.

EXPECTED FILE LAYOUT
--------------------
<exports_dir>/
    leads.json       — list of lead objects (REQUIRED for getLeads)
    pipelines.json   — list of pipeline objects (REQUIRED for getPipelines)
    contacts.json    — optional; silently skipped if missing
    users.json       — optional; silently skipped if missing

CORE JOIN KEY
-------------
`lead.responsible_user_id` identifies the agent responsible for a lead.
This value is the primary join key when correlating Kommo leads with
Rinkel call records (matched against agent.external_agent_id).

USAGE
-----
    from app.integrations.kommo import KommoProvider

    provider = KommoProvider()                        # uses default exports dir
    provider = KommoProvider("/custom/path/to/dir")   # explicit path

    leads     = provider.get_leads()
    pipelines = provider.get_pipelines()
    contacts  = provider.get_contacts()   # returns [] if file missing
    users     = provider.get_users()      # returns [] if file missing

    # Lookup helpers
    lead_map     = provider.leads_by_id()
    pipeline_map = provider.pipelines_by_id()
    user_map     = provider.users_by_id()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Default exports directory ──────────────────────────────────────────────────
# Resolves to   <project_root>/exports/
# Override at construction time for testing or alternative paths.
_DEFAULT_EXPORTS_DIR = Path(__file__).resolve().parents[2] / "exports"


class KommoProvider:
    """
    Read-only provider for Kommo CRM data stored as local JSON exports.

    All methods return plain Python dicts/lists parsed directly from the
    underlying JSON files.  No transformation, normalisation, or enrichment
    is performed — the raw Kommo data is returned as-is.

    The provider is safe to use when files are partially present:
    - `get_leads()` and `get_pipelines()` return an empty list when their
      respective file is absent or malformed (and log a warning).
    - `get_contacts()` and `get_users()` always return an empty list when
      the file is missing (these are optional sources).
    """

    # ── File names expected inside the exports directory ─────────────────────
    _FILE_LEADS     = "leads.json"
    _FILE_PIPELINES = "pipelines.json"
    _FILE_CONTACTS  = "contacts.json"   # optional
    _FILE_USERS     = "users.json"      # optional

    def __init__(self, exports_dir: str | Path | None = None) -> None:
        """
        Initialise the provider.

        Args:
            exports_dir: Path to the directory containing Kommo JSON exports.
                         Defaults to <project_root>/exports/.
        """
        self._dir = Path(exports_dir) if exports_dir is not None else _DEFAULT_EXPORTS_DIR
        logger.debug("KommoProvider initialised", extra={"exports_dir": str(self._dir)})

    # ── Public API ────────────────────────────────────────────────────────────

    def get_leads(self) -> list[dict[str, Any]]:
        """
        Return all leads from leads.json.

        Each lead object is the raw Kommo API shape, including:
            - id                   (int)  — Kommo lead ID
            - name                 (str)  — lead name / deal title
            - responsible_user_id  (int)  — agent identifier (core join key)
            - status_id            (int)  — current pipeline stage
            - pipeline_id          (int)  — owning pipeline
            - created_at           (int)  — unix timestamp
            - updated_at           (int)  — unix timestamp
            - custom_fields_values (list) — any custom field data

        Returns:
            list of lead dicts.  Returns [] if leads.json is missing or
            cannot be parsed — never raises.
        """
        return self._load_file(self._FILE_LEADS, required=True)

    def get_pipelines(self) -> list[dict[str, Any]]:
        """
        Return all pipelines from pipelines.json.

        Each pipeline object typically contains:
            - id       (int)  — Kommo pipeline ID
            - name     (str)  — pipeline name
            - statuses (list) — list of status/stage objects within the pipeline

        Returns:
            list of pipeline dicts.  Returns [] if pipelines.json is missing
            or cannot be parsed — never raises.
        """
        return self._load_file(self._FILE_PIPELINES, required=True)

    def get_contacts(self) -> list[dict[str, Any]]:
        """
        Return all contacts from contacts.json (optional file).

        Returns:
            list of contact dicts, or [] if contacts.json does not exist.
        """
        return self._load_file(self._FILE_CONTACTS, required=False)

    def get_users(self) -> list[dict[str, Any]]:
        """
        Return all users from users.json (optional file).

        Returns:
            list of user dicts, or [] if users.json does not exist.
        """
        return self._load_file(self._FILE_USERS, required=False)

    # ── Lookup helpers ────────────────────────────────────────────────────────

    def leads_by_id(self) -> dict[int, dict[str, Any]]:
        """
        Return a dict mapping lead ID → lead object.

        Useful for O(1) lookups when joining with Rinkel call records.

        Returns:
            {lead_id: lead_dict, ...}
        """
        return {
            lead["id"]: lead
            for lead in self.get_leads()
            if isinstance(lead.get("id"), int)
        }

    def leads_by_responsible_user(self) -> dict[int, list[dict[str, Any]]]:
        """
        Return a dict mapping responsible_user_id → list of leads.

        This is the primary join map used to correlate Kommo leads with
        Rinkel agents via `lead.responsible_user_id`.

        Returns:
            {responsible_user_id: [lead_dict, ...], ...}
        """
        result: dict[int, list[dict[str, Any]]] = {}
        for lead in self.get_leads():
            uid = lead.get("responsible_user_id")
            if not isinstance(uid, int):
                continue
            result.setdefault(uid, []).append(lead)
        return result

    def pipelines_by_id(self) -> dict[int, dict[str, Any]]:
        """
        Return a dict mapping pipeline ID → pipeline object.

        Returns:
            {pipeline_id: pipeline_dict, ...}
        """
        return {
            pipeline["id"]: pipeline
            for pipeline in self.get_pipelines()
            if isinstance(pipeline.get("id"), int)
        }

    def users_by_id(self) -> dict[int, dict[str, Any]]:
        """
        Return a dict mapping user ID → user object (from users.json).

        Returns:
            {user_id: user_dict, ...}  — empty dict if users.json missing.
        """
        return {
            user["id"]: user
            for user in self.get_users()
            if isinstance(user.get("id"), int)
        }

    # ── Introspection ─────────────────────────────────────────────────────────

    def available_files(self) -> dict[str, bool]:
        """
        Return a dict showing which expected export files are present on disk.

        Useful for diagnostics and health checks.

        Returns:
            {filename: exists_bool, ...}
        """
        all_files = [
            self._FILE_LEADS,
            self._FILE_PIPELINES,
            self._FILE_CONTACTS,
            self._FILE_USERS,
        ]
        return {
            name: (self._dir / name).is_file()
            for name in all_files
        }

    def exports_dir(self) -> Path:
        """Return the resolved exports directory path."""
        return self._dir

    def __repr__(self) -> str:
        return f"KommoProvider(exports_dir={str(self._dir)!r})"

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_file(
        self,
        filename: str,
        *,
        required: bool,
    ) -> list[dict[str, Any]]:
        """
        Load and parse a single JSON file from the exports directory.

        The method:
        1. Resolves the full file path.
        2. If the file does not exist:
           - `required=True`  → logs a WARNING and returns [].
           - `required=False` → logs a DEBUG message and returns [].
        3. If the file exists but cannot be decoded or is not a list:
           - Logs an ERROR and returns [] — never propagates the exception.

        Args:
            filename: Name of the JSON file (e.g. "leads.json").
            required: Whether the file is expected to exist.

        Returns:
            list of dicts, or [] on any failure.
        """
        path = self._dir / filename

        # ── File existence check ──────────────────────────────────────────
        if not path.exists():
            if required:
                logger.warning(
                    "Kommo export file not found",
                    extra={"file": str(path)},
                )
            else:
                logger.debug(
                    "Optional Kommo export file absent — skipping",
                    extra={"file": str(path)},
                )
            return []

        if not path.is_file():
            logger.error(
                "Kommo export path exists but is not a file",
                extra={"path": str(path)},
            )
            return []

        # ── Read & parse ──────────────────────────────────────────────────
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(
                "Failed to read Kommo export file",
                extra={"file": str(path), "error": str(exc)},
            )
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse Kommo export file as JSON",
                extra={"file": str(path), "error": str(exc)},
            )
            return []

        # ── Shape validation ──────────────────────────────────────────────
        # Kommo API exports can be wrapped in an envelope like:
        #   {"_embedded": {"leads": [...]}}    (v4 API format)
        #   [...]                               (direct list export)
        #
        # We normalise both shapes to a flat list.
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            # Try common Kommo API envelope keys
            key = filename.replace(".json", "")  # e.g. "leads" from "leads.json"
            embedded = data.get("_embedded", {})
            if isinstance(embedded, dict) and key in embedded:
                records = embedded[key]
            elif key in data:
                records = data[key]
            else:
                # Return the dict wrapped in a list if no known key found
                logger.warning(
                    "Kommo export file is a dict with no recognised envelope key; "
                    "wrapping in list",
                    extra={"file": str(path), "keys": list(data.keys())},
                )
                records = [data]
        else:
            logger.error(
                "Kommo export file has unexpected top-level type; expected list or dict",
                extra={"file": str(path), "type": type(data).__name__},
            )
            return []

        if not isinstance(records, list):
            logger.error(
                "Kommo export records resolved to a non-list type",
                extra={"file": str(path), "type": type(records).__name__},
            )
            return []

        logger.debug(
            "Loaded Kommo export file",
            extra={"file": filename, "record_count": len(records)},
        )
        return records
