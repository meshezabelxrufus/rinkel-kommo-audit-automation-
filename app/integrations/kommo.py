"""
KommoProvider — read-only data provider for Kommo CRM local JSON exports.

PURPOSE
-------
Reads pre-exported Kommo JSON files from the local `exports/` directory.
This is a pure file-system provider: it makes no API calls, touches no
database, and performs no ingestion.  It is intentionally read-only.

REAL EXPORT FORMAT (kommo_api_v4 custom extractor)
---------------------------------------------------
Every file produced by the real extraction pipeline uses the envelope:

    {
        "_meta": {
            "entity": "leads",
            "count": 448,
            "extracted_at": "2026-05-28T06:00:09Z",
            "source": "kommo_api_v4"
        },
        "data": [ <record>, <record>, ... ]
    }

The provider unwraps this envelope automatically.
It also handles the standard Kommo v4 API "_embedded" envelope and raw
list exports, so it works with any export format.

FILE LAYOUT
-----------
<exports_dir>/
    leads.json          — 448 leads (REQUIRED for get_leads)
    pipelines.json      — 11 pipelines, 110 stages (REQUIRED for get_pipelines)
    chats.json          — 321 chat/talk records (optional)
    messages_flat.json  — 154 flat AI-ready message records (optional)

CORE JOIN KEY
-------------
`lead.responsible_user_id`   — links a lead to the agent who owns it.
`message.author_id`          — links a message to the Kommo user who sent it.
Both values match the Kommo user/agent integer ID.

USAGE
-----
    from app.integrations.kommo import KommoProvider

    provider = KommoProvider()               # uses <project_root>/exports/
    provider = KommoProvider("/other/path")  # explicit path

    leads     = provider.get_leads()
    pipelines = provider.get_pipelines()
    chats     = provider.get_chats()
    messages  = provider.get_messages()

    # O(1) lookup helpers
    by_agent  = provider.leads_by_responsible_user()  # {user_id: [lead,...]}
    by_lead   = provider.messages_by_lead_id()        # {lead_id: [msg,...]}
    p_map     = provider.pipelines_by_id()            # {pipeline_id: pipeline}
    stage_map = provider.stages_by_id()               # {stage_id: stage}

    print(provider.meta("leads"))            # {"entity": ..., "count": 448}
    print(provider.available_files())        # {filename: exists_bool}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Default exports directory ──────────────────────────────────────────────────
_DEFAULT_EXPORTS_DIR = Path(__file__).resolve().parents[2] / "exports"


class KommoProvider:
    """
    Read-only provider for Kommo CRM data stored as local JSON exports.

    All methods return plain Python dicts/lists parsed directly from the
    underlying JSON files.  No transformation or normalisation is applied —
    raw Kommo data is returned as-is so callers can rely on the original
    field names and types.

    Safety guarantees:
    - Every method returns [] / {} — never raises on missing/malformed files.
    - Required files (leads, pipelines) log a WARNING when absent.
    - Optional files (chats, messages) log a DEBUG message when absent.
    """

    # ── Known file names ──────────────────────────────────────────────────────
    _FILE_LEADS     = "leads.json"
    _FILE_PIPELINES = "pipelines.json"
    _FILE_CHATS     = "chats.json"          # optional
    _FILE_MESSAGES  = "messages_flat.json"  # optional

    def __init__(self, exports_dir: str | Path | None = None) -> None:
        self._dir = Path(exports_dir) if exports_dir is not None else _DEFAULT_EXPORTS_DIR
        # Cache: filename → (records, meta)
        self._cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        logger.debug("KommoProvider initialised", extra={"exports_dir": str(self._dir)})

    # ─────────────────────────────────────────────────────────────────────────
    # Primary accessors
    # ─────────────────────────────────────────────────────────────────────────

    def get_leads(self) -> list[dict[str, Any]]:
        """
        Return all leads from leads.json.

        Real record shape (kommo_api_v4):
            id                   int   — Kommo lead ID
            name                 str   — deal/lead title
            pipeline_id          int   — owning pipeline
            status_id            int   — current stage
            responsible_user_id  int   — agent (CORE JOIN KEY)
            created_at           int   — unix timestamp
            updated_at           int   — unix timestamp
            closed_at            int|null
            price                float
            loss_reason_id       int|null
            is_deleted           bool
            score                int|null
            custom_fields_values list

        Returns [] if leads.json is missing or malformed.
        """
        return self._load(self._FILE_LEADS, required=True)

    def get_pipelines(self) -> list[dict[str, Any]]:
        """
        Return all pipelines from pipelines.json.

        Real record shape (kommo_api_v4):
            pipeline_id    int   — Kommo pipeline ID
            pipeline_name  str   — pipeline name
            sort           int
            is_main        bool
            is_archive     bool
            account_id     int
            stages         list  — list of stage objects:
                               stage_id, stage_name, pipeline_id, sort, color
            total_stages   int
            regular_stages int

        Returns [] if pipelines.json is missing or malformed.
        """
        return self._load(self._FILE_PIPELINES, required=True)

    def get_chats(self) -> list[dict[str, Any]]:
        """
        Return all chat/talk records from chats.json (optional).

        Real record shape:
            id               str   — UUID chat ID
            entity_id        int   — linked lead ID
            entity_type      str   — 'lead'
            lead_name        str
            contact_name     str|null
            channel_type     str   — 'waba', 'email', etc.
            channel          str   — human-readable channel name
            talk_id          int
            last_message_at  int   — unix timestamp
            total_messages   int
            inbound          int
            outbound         int
            extraction_source str

        Returns [] if chats.json is absent.
        """
        return self._load(self._FILE_CHATS, required=False)

    def get_messages(self) -> list[dict[str, Any]]:
        """
        Return all flat message records from messages_flat.json (optional).

        AI-ready flat schema extracted via fallback chain (events + notes + talks).

        Real record shape:
            lead_id          int   — linked lead ID
            lead_name        str|null
            contact_name     str|null
            channel          str   — 'WhatsApp Business API', 'Internal Note', etc.
            direction        str   — 'inbound' | 'outbound'
            author           str   — author user ID (as string)
            author_id        int   — author user ID (as int) — AGENT JOIN KEY
            author_type      str   — 'user' | 'contact'
            message_text     str
            timestamp        int   — unix timestamp
            timestamp_iso    str   — ISO 8601
            message_id       str
            talk_id          int
            chat_id          str
            channel_raw      str
            media_url        str|null
            extraction_source str

        Returns [] if messages_flat.json is absent.
        """
        return self._load(self._FILE_MESSAGES, required=False)

    # ─────────────────────────────────────────────────────────────────────────
    # Metadata accessor
    # ─────────────────────────────────────────────────────────────────────────

    def meta(self, entity: str) -> dict[str, Any]:
        """
        Return the _meta block from the given file's envelope.

        Args:
            entity: one of "leads", "pipelines", "chats", "messages"

        Returns:
            dict with keys like: entity, count, extracted_at, source
            Returns {} if the file is missing or has no _meta block.

        Example:
            provider.meta("leads")
            # {"entity": "leads", "count": 448,
            #  "extracted_at": "2026-05-28T06:00:09Z", "source": "kommo_api_v4"}
        """
        file_map = {
            "leads":     self._FILE_LEADS,
            "pipelines": self._FILE_PIPELINES,
            "chats":     self._FILE_CHATS,
            "messages":  self._FILE_MESSAGES,
        }
        fname = file_map.get(entity)
        if fname is None:
            logger.warning("KommoProvider.meta: unknown entity %r", entity)
            return {}
        self._load(fname, required=False)  # ensure cached
        _, m = self._cache.get(fname, ([], {}))
        return m

    # ─────────────────────────────────────────────────────────────────────────
    # Lookup / join helpers
    # ─────────────────────────────────────────────────────────────────────────

    def leads_by_id(self) -> dict[int, dict[str, Any]]:
        """
        Map lead_id → lead dict.  O(1) lookup.

        Returns:
            {lead_id (int): lead_dict}
        """
        return {
            lead["id"]: lead
            for lead in self.get_leads()
            if isinstance(lead.get("id"), int)
        }

    def leads_by_responsible_user(self) -> dict[int, list[dict[str, Any]]]:
        """
        Map responsible_user_id → list of lead dicts.

        This is the PRIMARY join map for correlating Kommo leads with
        Rinkel call agents.  The `responsible_user_id` on each lead matches
        the Kommo user/agent integer ID.

        Returns:
            {responsible_user_id (int): [lead_dict, ...]}
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
        Map pipeline_id → pipeline dict.  O(1) lookup.

        Note: real data uses the key `pipeline_id`, not `id`.

        Returns:
            {pipeline_id (int): pipeline_dict}
        """
        result = {}
        for p in self.get_pipelines():
            pid = p.get("pipeline_id") or p.get("id")
            if isinstance(pid, int):
                result[pid] = p
        return result

    def stages_by_id(self) -> dict[int, dict[str, Any]]:
        """
        Flatten all pipeline stages into a single map: stage_id → stage dict.

        Each stage dict includes the parent pipeline context, allowing a
        complete stage lookup from just a `status_id` on a lead.

        Returns:
            {stage_id (int): stage_dict}
        """
        result: dict[int, dict[str, Any]] = {}
        for pipeline in self.get_pipelines():
            for stage in pipeline.get("stages", []):
                sid = stage.get("stage_id") or stage.get("id")
                if isinstance(sid, int):
                    result[sid] = stage
        return result

    def chats_by_lead_id(self) -> dict[int, list[dict[str, Any]]]:
        """
        Map entity_id (lead ID) → list of chat records.

        Returns:
            {lead_id (int): [chat_dict, ...]}
        """
        result: dict[int, list[dict[str, Any]]] = {}
        for chat in self.get_chats():
            lid = chat.get("entity_id")
            if not isinstance(lid, int):
                continue
            result.setdefault(lid, []).append(chat)
        return result

    def messages_by_lead_id(self) -> dict[int, list[dict[str, Any]]]:
        """
        Map lead_id → list of message records.

        Returns:
            {lead_id (int): [message_dict, ...]}
        """
        result: dict[int, list[dict[str, Any]]] = {}
        for msg in self.get_messages():
            lid = msg.get("lead_id")
            if not isinstance(lid, int):
                continue
            result.setdefault(lid, []).append(msg)
        return result

    def messages_by_author(self) -> dict[int, list[dict[str, Any]]]:
        """
        Map author_id (Kommo user/agent int ID) → list of message records.

        Useful for agent-level message analytics.

        Returns:
            {author_id (int): [message_dict, ...]}
        """
        result: dict[int, list[dict[str, Any]]] = {}
        for msg in self.get_messages():
            aid = msg.get("author_id")
            if not isinstance(aid, int):
                continue
            result.setdefault(aid, []).append(msg)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Introspection
    # ─────────────────────────────────────────────────────────────────────────

    def available_files(self) -> dict[str, bool]:
        """
        Return a dict showing which expected export files exist on disk.

        Returns:
            {"leads.json": True, "pipelines.json": True, ...}
        """
        return {
            name: (self._dir / name).is_file()
            for name in [
                self._FILE_LEADS,
                self._FILE_PIPELINES,
                self._FILE_CHATS,
                self._FILE_MESSAGES,
            ]
        }

    def exports_dir(self) -> Path:
        """Return the resolved exports directory path."""
        return self._dir

    def __repr__(self) -> str:
        return f"KommoProvider(exports_dir={str(self._dir)!r})"

    # ─────────────────────────────────────────────────────────────────────────
    # Private
    # ─────────────────────────────────────────────────────────────────────────

    def _load(
        self,
        filename: str,
        *,
        required: bool,
    ) -> list[dict[str, Any]]:
        """
        Load and parse a single JSON file, with caching and full error handling.

        Envelope unwrapping order (first match wins):
            1. {"_meta": {...}, "data": [...]}       ← real export format
            2. {"_embedded": {"<key>": [...]}}        ← Kommo v4 API format
            3. {"<filename_stem>": [...]}             ← top-level key format
            4. [...]                                  ← bare list

        Returns [] on any failure — never raises.
        """
        if filename in self._cache:
            records, _ = self._cache[filename]
            return records

        path = self._dir / filename

        # ── Existence ─────────────────────────────────────────────────────
        if not path.exists():
            if required:
                logger.warning(
                    "Required Kommo export file not found",
                    extra={"file": str(path)},
                )
            else:
                logger.debug(
                    "Optional Kommo export file absent — skipping",
                    extra={"file": str(path)},
                )
            self._cache[filename] = ([], {})
            return []

        if not path.is_file():
            logger.error(
                "Kommo export path is not a regular file",
                extra={"path": str(path)},
            )
            self._cache[filename] = ([], {})
            return []

        # ── Read ──────────────────────────────────────────────────────────
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(
                "Failed to read Kommo export file",
                extra={"file": str(path), "error": str(exc)},
            )
            self._cache[filename] = ([], {})
            return []

        # ── Parse ─────────────────────────────────────────────────────────
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse Kommo export file as JSON",
                extra={"file": str(path), "error": str(exc)},
            )
            self._cache[filename] = ([], {})
            return []

        # ── Unwrap envelope ───────────────────────────────────────────────
        meta: dict[str, Any] = {}
        records: list[Any] = []

        if isinstance(data, list):
            # Bare list — no envelope
            records = data

        elif isinstance(data, dict):
            # 1. Real export format: {"_meta": {...}, "data": [...]}
            if "data" in data and isinstance(data["data"], list):
                meta = data.get("_meta", {})
                records = data["data"]

            # 2. Kommo v4 _embedded: {"_embedded": {"leads": [...]}}
            elif "_embedded" in data and isinstance(data["_embedded"], dict):
                embedded = data["_embedded"]
                key = filename.replace(".json", "")
                if key in embedded and isinstance(embedded[key], list):
                    records = embedded[key]
                else:
                    # Take the first list-valued key in _embedded
                    for v in embedded.values():
                        if isinstance(v, list):
                            records = v
                            break

            # 3. Top-level key: {"leads": [...]}
            else:
                key = filename.replace(".json", "")
                if key in data and isinstance(data[key], list):
                    records = data[key]
                else:
                    # Fallback: wrap the whole dict in a list
                    logger.warning(
                        "Kommo export dict has no recognised envelope key; "
                        "wrapping in list",
                        extra={"file": str(path), "top_keys": list(data.keys())[:8]},
                    )
                    records = [data]
        else:
            logger.error(
                "Kommo export has unexpected top-level type",
                extra={"file": str(path), "type": type(data).__name__},
            )
            self._cache[filename] = ([], {})
            return []

        if not isinstance(records, list):
            logger.error(
                "Kommo export records resolved to non-list",
                extra={"file": str(path), "type": type(records).__name__},
            )
            self._cache[filename] = ([], {})
            return []

        logger.debug(
            "Loaded Kommo export",
            extra={
                "file": filename,
                "records": len(records),
                "entity": meta.get("entity", "unknown"),
                "extracted_at": meta.get("extracted_at"),
            },
        )
        self._cache[filename] = (records, meta)
        return records
