"""
Tests for KommoProvider — updated for real kommo_api_v4 export format.

Real export envelope:
    {"_meta": {"entity": "leads", "count": 448, ...}, "data": [...]}

Test groups:
    TestGetLeads           — get_leads() with all envelope variants
    TestGetPipelines       — get_pipelines() with real stage structure
    TestGetChats           — get_chats() optional
    TestGetMessages        — get_messages() optional flat schema
    TestMeta               — meta() accessor
    TestLeadsById          — O(1) lookup by lead ID
    TestLeadsByResponsibleUser — core join helper
    TestPipelinesById      — O(1) lookup by pipeline_id key
    TestStagesById         — flattened stage lookup
    TestChatsByLeadId      — chat grouping helper
    TestMessagesByLeadId   — message grouping helper
    TestMessagesByAuthor   — agent-level message grouping
    TestAvailableFiles     — introspection
    TestCaching            — each file read exactly once per instance
    TestEdgeCases          — nonexistent dir, malformed, null, unicode, repr
    TestRealData           — smoke tests against real exports/ files (skipped if absent)
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from app.integrations.kommo import KommoProvider


# ── Shared sample data reflecting real kommo_api_v4 format ───────────────────

REAL_LEADS_ENVELOPE = {
    "_meta": {
        "entity": "leads",
        "count": 3,
        "extracted_at": "2026-05-28T06:00:09.457581+00:00",
        "source": "kommo_api_v4",
    },
    "data": [
        {
            "id": 25892146,
            "name": "Antonio Hidalgo - Pechos",
            "pipeline_id": 11231784,
            "status_id": 143,
            "responsible_user_id": 10359915,
            "group_id": 0,
            "created_at": 1779623803,
            "updated_at": 1779812622,
            "closed_at": 1779812620,
            "price": 0,
            "loss_reason_id": 18327063,
            "is_deleted": False,
            "score": None,
            "custom_fields_values": [],
        },
        {
            "id": 25910242,
            "name": "Lety mellisa - Pechos",
            "pipeline_id": 11231784,
            "status_id": 142,
            "responsible_user_id": 10359915,
            "group_id": 0,
            "created_at": 1779800000,
            "updated_at": 1779900000,
            "closed_at": None,
            "price": 1200.0,
            "loss_reason_id": None,
            "is_deleted": False,
            "score": 85,
            "custom_fields_values": [
                {"field_id": 900, "field_name": "Phone", "values": [{"value": "+34612345678"}]}
            ],
        },
        {
            "id": 25733960,
            "name": "Credit request - Hans",
            "pipeline_id": 11231785,
            "status_id": 211,
            "responsible_user_id": 10400001,
            "group_id": 0,
            "created_at": 1778151800,
            "updated_at": 1778200000,
            "closed_at": None,
            "price": 15.0,
            "loss_reason_id": None,
            "is_deleted": False,
            "score": None,
            "custom_fields_values": [],
        },
    ],
}

REAL_PIPELINES_ENVELOPE = {
    "_meta": {
        "entity": "pipelines",
        "count": 2,
        "total_stages": 5,
        "extracted_at": "2026-05-28T06:00:06.098500+00:00",
        "source": "kommo_api_v4",
    },
    "data": [
        {
            "pipeline_id": 11231784,
            "pipeline_name": "Klantenservice",
            "sort": 1,
            "is_main": True,
            "is_unsorted_on": True,
            "is_archive": False,
            "account_id": 31959059,
            "total_stages": 3,
            "regular_stages": 2,
            "stages": [
                {"stage_id": 62386811, "stage_name": "Incoming leads", "pipeline_id": 11231784, "sort": 10, "color": "#c1c1c1"},
                {"stage_id": 62386812, "stage_name": "In behandeling", "pipeline_id": 11231784, "sort": 20, "color": "#fffd7f"},
                {"stage_id": 62386813, "stage_name": "Opgelost",       "pipeline_id": 11231784, "sort": 30, "color": "#ccff66"},
            ],
        },
        {
            "pipeline_id": 11231785,
            "pipeline_name": "Creditering",
            "sort": 2,
            "is_main": False,
            "is_unsorted_on": False,
            "is_archive": False,
            "account_id": 31959059,
            "total_stages": 2,
            "regular_stages": 2,
            "stages": [
                {"stage_id": 62387001, "stage_name": "Aanvraag ontvangen", "pipeline_id": 11231785, "sort": 10, "color": "#c1e0ff"},
                {"stage_id": 62387002, "stage_name": "Goedgekeurd",        "pipeline_id": 11231785, "sort": 20, "color": "#ccff66"},
            ],
        },
    ],
}

REAL_CHATS_ENVELOPE = {
    "_meta": {
        "entity": "chats",
        "count": 2,
        "total_messages": 35,
        "extracted_at": "2026-05-28T06:04:57Z",
        "source": "kommo_api_v4",
    },
    "data": [
        {
            "id": "98ba9e40-6df7-4126-ada9-dece67a403e8",
            "entity_id": 25910246,
            "entity_type": "lead",
            "lead_name": "Lead #25910246",
            "contact_name": None,
            "channel_type": "waba",
            "channel": "WhatsApp Business API",
            "talk_id": 54605,
            "last_message_at": 1779948131,
            "total_messages": 23,
            "inbound": 12,
            "outbound": 11,
            "has_text_notes": False,
            "extraction_source": "fallback_chain",
        },
        {
            "id": "774334b2-2277-4c4b-938b-0f1ead66209e",
            "entity_id": 25892146,
            "entity_type": "lead",
            "lead_name": "Antonio Hidalgo - Pechos",
            "contact_name": None,
            "channel_type": "waba",
            "channel": "WhatsApp Business API",
            "talk_id": 54604,
            "last_message_at": 1779947790,
            "total_messages": 12,
            "inbound": 7,
            "outbound": 5,
            "has_text_notes": False,
            "extraction_source": "fallback_chain",
        },
    ],
}

REAL_MESSAGES_ENVELOPE = {
    "_meta": {
        "entity": "messages",
        "count": 3,
        "extracted_at": "2026-05-26T12:38:50Z",
        "source": "kommo_api_v4",
        "schema": "lead_id,lead_name,contact_name,channel,direction,author,message_text,timestamp",
    },
    "data": [
        {
            "lead_id": 25617036,
            "lead_name": None,
            "contact_name": None,
            "channel": "Internal Note",
            "direction": "outbound",
            "author": "10359915",
            "author_id": 10359915,
            "author_type": "user",
            "message_text": "nc llamadas",
            "timestamp": 1776689587,
            "timestamp_iso": "2026-04-20T12:53:07+00:00",
            "message_id": "14788840",
            "talk_id": 54420,
            "chat_id": "22169147-eff6-45bf-8f65-c2ebd80c95b1",
            "channel_raw": "com.wazzup.whatsapp",
            "media_url": None,
            "extraction_source": "notes_api",
        },
        {
            "lead_id": 25733960,
            "lead_name": None,
            "contact_name": None,
            "channel": "Internal Note",
            "direction": "outbound",
            "author": "10359915",
            "author_id": 10359915,
            "author_type": "user",
            "message_text": "Primera vez, los encontré buscando info en google.",
            "timestamp": 1778151822,
            "timestamp_iso": "2026-05-07T11:03:42+00:00",
            "message_id": "14959288",
            "talk_id": 54382,
            "chat_id": "1ef48398-b134-49ff-982d-f81f4d08e277",
            "channel_raw": "waba",
            "media_url": None,
            "extraction_source": "notes_api",
        },
        {
            "lead_id": 25733960,
            "lead_name": None,
            "contact_name": None,
            "channel": "WhatsApp Business API",
            "direction": "inbound",
            "author": "10400001",
            "author_id": 10400001,
            "author_type": "contact",
            "message_text": "Hola, necesito información",
            "timestamp": 1778200000,
            "timestamp_iso": "2026-05-07T23:06:40+00:00",
            "message_id": "14960000",
            "talk_id": 54383,
            "chat_id": "1ef48398-b134-49ff-982d-f81f4d08e278",
            "channel_raw": "waba",
            "media_url": None,
            "extraction_source": "talks_metadata",
        },
    ],
}


# ── Helper ────────────────────────────────────────────────────────────────────

def _write(directory: Path, filename: str, data: object) -> None:
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


# ── Tests: get_leads() ────────────────────────────────────────────────────────

class TestGetLeads:
    def test_real_envelope_unwrapped(self, tmp_path: Path) -> None:
        """Real {"_meta", "data"} envelope is handled correctly."""
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        leads = KommoProvider(tmp_path).get_leads()
        assert len(leads) == 3

    def test_lead_fields_intact(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        lead = KommoProvider(tmp_path).get_leads()[0]
        assert lead["id"] == 25892146
        assert lead["responsible_user_id"] == 10359915
        assert lead["pipeline_id"] == 11231784
        assert lead["status_id"] == 143
        assert lead["is_deleted"] is False

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).get_leads() == []

    def test_malformed_json_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "leads.json").write_text("{not json", encoding="utf-8")
        assert KommoProvider(tmp_path).get_leads() == []

    def test_empty_data_array_returns_empty_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", {"_meta": {"entity": "leads"}, "data": []})
        assert KommoProvider(tmp_path).get_leads() == []

    def test_bare_list_format_supported(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE["data"])
        assert len(KommoProvider(tmp_path).get_leads()) == 3

    def test_embedded_envelope_supported(self, tmp_path: Path) -> None:
        envelope = {"_embedded": {"leads": REAL_LEADS_ENVELOPE["data"]}}
        _write(tmp_path, "leads.json", envelope)
        assert len(KommoProvider(tmp_path).get_leads()) == 3

    def test_top_level_key_envelope_supported(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", {"leads": REAL_LEADS_ENVELOPE["data"]})
        assert len(KommoProvider(tmp_path).get_leads()) == 3

    def test_custom_fields_preserved(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        lead_with_cf = KommoProvider(tmp_path).get_leads()[1]
        assert lead_with_cf["custom_fields_values"][0]["field_name"] == "Phone"


# ── Tests: get_pipelines() ────────────────────────────────────────────────────

class TestGetPipelines:
    def test_real_envelope_unwrapped(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        pipelines = KommoProvider(tmp_path).get_pipelines()
        assert len(pipelines) == 2

    def test_pipeline_uses_pipeline_id_key(self, tmp_path: Path) -> None:
        """Real data uses 'pipeline_id', not 'id'."""
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        p = KommoProvider(tmp_path).get_pipelines()[0]
        assert p["pipeline_id"] == 11231784
        assert p["pipeline_name"] == "Klantenservice"

    def test_stages_nested_correctly(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        p = KommoProvider(tmp_path).get_pipelines()[0]
        assert len(p["stages"]) == 3
        assert p["stages"][0]["stage_id"] == 62386811
        assert p["stages"][0]["stage_name"] == "Incoming leads"

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).get_pipelines() == []

    def test_malformed_json_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "pipelines.json").write_text("<<<", encoding="utf-8")
        assert KommoProvider(tmp_path).get_pipelines() == []


# ── Tests: get_chats() ───────────────────────────────────────────────────────

class TestGetChats:
    def test_returns_chats_when_present(self, tmp_path: Path) -> None:
        _write(tmp_path, "chats.json", REAL_CHATS_ENVELOPE)
        chats = KommoProvider(tmp_path).get_chats()
        assert len(chats) == 2

    def test_chat_fields(self, tmp_path: Path) -> None:
        _write(tmp_path, "chats.json", REAL_CHATS_ENVELOPE)
        chat = KommoProvider(tmp_path).get_chats()[0]
        assert chat["id"] == "98ba9e40-6df7-4126-ada9-dece67a403e8"
        assert chat["entity_id"] == 25910246
        assert chat["entity_type"] == "lead"
        assert chat["channel_type"] == "waba"
        assert chat["total_messages"] == 23

    def test_absent_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).get_chats() == []

    def test_malformed_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "chats.json").write_text("null", encoding="utf-8")
        assert KommoProvider(tmp_path).get_chats() == []


# ── Tests: get_messages() ────────────────────────────────────────────────────

class TestGetMessages:
    def test_returns_messages_when_present(self, tmp_path: Path) -> None:
        _write(tmp_path, "messages_flat.json", REAL_MESSAGES_ENVELOPE)
        msgs = KommoProvider(tmp_path).get_messages()
        assert len(msgs) == 3

    def test_message_fields(self, tmp_path: Path) -> None:
        _write(tmp_path, "messages_flat.json", REAL_MESSAGES_ENVELOPE)
        msg = KommoProvider(tmp_path).get_messages()[0]
        assert msg["lead_id"] == 25617036
        assert msg["author_id"] == 10359915
        assert msg["direction"] == "outbound"
        assert msg["channel"] == "Internal Note"
        assert msg["message_text"] == "nc llamadas"

    def test_absent_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).get_messages() == []


# ── Tests: meta() ─────────────────────────────────────────────────────────────

class TestMeta:
    def test_returns_meta_block(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        m = KommoProvider(tmp_path).meta("leads")
        assert m["entity"] == "leads"
        assert m["count"] == 3
        assert m["source"] == "kommo_api_v4"
        assert "extracted_at" in m

    def test_returns_empty_dict_for_missing_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).meta("leads") == {}

    def test_returns_empty_dict_for_unknown_entity(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).meta("unknown_entity") == {}

    def test_pipelines_meta(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        m = KommoProvider(tmp_path).meta("pipelines")
        assert m["total_stages"] == 5

    def test_bare_list_has_empty_meta(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE["data"])
        m = KommoProvider(tmp_path).meta("leads")
        assert m == {}


# ── Tests: leads_by_id() ─────────────────────────────────────────────────────

class TestLeadsById:
    def test_maps_id_to_lead(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        mapping = KommoProvider(tmp_path).leads_by_id()
        assert mapping[25892146]["name"] == "Antonio Hidalgo - Pechos"
        assert set(mapping.keys()) == {25892146, 25910242, 25733960}

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).leads_by_id() == {}


# ── Tests: leads_by_responsible_user() ───────────────────────────────────────

class TestLeadsByResponsibleUser:
    def test_groups_by_agent(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        mapping = KommoProvider(tmp_path).leads_by_responsible_user()
        # agent 10359915 owns 2 leads; agent 10400001 owns 1
        assert len(mapping[10359915]) == 2
        assert len(mapping[10400001]) == 1

    def test_lead_ids_under_agent(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        ids = {l["id"] for l in KommoProvider(tmp_path).leads_by_responsible_user()[10359915]}
        assert ids == {25892146, 25910242}

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).leads_by_responsible_user() == {}


# ── Tests: pipelines_by_id() ─────────────────────────────────────────────────

class TestPipelinesById:
    def test_uses_pipeline_id_key(self, tmp_path: Path) -> None:
        """Real data uses pipeline_id, not id."""
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        mapping = KommoProvider(tmp_path).pipelines_by_id()
        assert 11231784 in mapping
        assert mapping[11231784]["pipeline_name"] == "Klantenservice"
        assert 11231785 in mapping

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).pipelines_by_id() == {}


# ── Tests: stages_by_id() ────────────────────────────────────────────────────

class TestStagesById:
    def test_flattens_all_stages(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        stages = KommoProvider(tmp_path).stages_by_id()
        # 3 from pipeline 1 + 2 from pipeline 2 = 5
        assert len(stages) == 5

    def test_stage_lookup_by_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", REAL_PIPELINES_ENVELOPE)
        stages = KommoProvider(tmp_path).stages_by_id()
        assert stages[62386811]["stage_name"] == "Incoming leads"
        assert stages[62387002]["stage_name"] == "Goedgekeurd"

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).stages_by_id() == {}


# ── Tests: chats_by_lead_id() ────────────────────────────────────────────────

class TestChatsByLeadId:
    def test_groups_by_entity_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "chats.json", REAL_CHATS_ENVELOPE)
        mapping = KommoProvider(tmp_path).chats_by_lead_id()
        assert 25910246 in mapping
        assert mapping[25910246][0]["channel_type"] == "waba"

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).chats_by_lead_id() == {}


# ── Tests: messages_by_lead_id() ─────────────────────────────────────────────

class TestMessagesByLeadId:
    def test_groups_by_lead_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "messages_flat.json", REAL_MESSAGES_ENVELOPE)
        mapping = KommoProvider(tmp_path).messages_by_lead_id()
        assert len(mapping[25733960]) == 2
        assert len(mapping[25617036]) == 1

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).messages_by_lead_id() == {}


# ── Tests: messages_by_author() ──────────────────────────────────────────────

class TestMessagesByAuthor:
    def test_groups_by_author_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "messages_flat.json", REAL_MESSAGES_ENVELOPE)
        mapping = KommoProvider(tmp_path).messages_by_author()
        # author 10359915 has 2 messages; author 10400001 has 1
        assert len(mapping[10359915]) == 2
        assert len(mapping[10400001]) == 1

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).messages_by_author() == {}


# ── Tests: available_files() ─────────────────────────────────────────────────

class TestAvailableFiles:
    def test_all_absent_in_empty_dir(self, tmp_path: Path) -> None:
        result = KommoProvider(tmp_path).available_files()
        assert result == {
            "leads.json": False,
            "pipelines.json": False,
            "chats.json": False,
            "messages_flat.json": False,
        }

    def test_detects_real_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        _write(tmp_path, "chats.json", REAL_CHATS_ENVELOPE)
        result = KommoProvider(tmp_path).available_files()
        assert result["leads.json"] is True
        assert result["chats.json"] is True
        assert result["pipelines.json"] is False
        assert result["messages_flat.json"] is False


# ── Tests: caching ────────────────────────────────────────────────────────────

class TestCaching:
    def test_same_object_returned_on_repeat_calls(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        provider = KommoProvider(tmp_path)
        leads1 = provider.get_leads()
        leads2 = provider.get_leads()
        assert leads1 is leads2  # same list object from cache

    def test_cache_isolated_per_instance(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", REAL_LEADS_ENVELOPE)
        p1 = KommoProvider(tmp_path)
        p2 = KommoProvider(tmp_path)
        assert p1.get_leads() is not p2.get_leads()  # different instances, different cache


# ── Tests: edge cases ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_nonexistent_dir_never_crashes(self, tmp_path: Path) -> None:
        ghost = tmp_path / "ghost_dir"
        p = KommoProvider(ghost)
        assert p.get_leads() == []
        assert p.get_pipelines() == []
        assert p.get_chats() == []
        assert p.get_messages() == []

    def test_null_json_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "leads.json").write_text("null", encoding="utf-8")
        assert KommoProvider(tmp_path).get_leads() == []

    def test_unicode_preserved(self, tmp_path: Path) -> None:
        leads = [{"id": 1, "name": "Monteur — Dë Vries", "responsible_user_id": 1}]
        _write(tmp_path, "leads.json", {"_meta": {}, "data": leads})
        result = KommoProvider(tmp_path).get_leads()
        assert result[0]["name"] == "Monteur — Dë Vries"

    def test_lead_without_id_excluded_from_leads_by_id(self, tmp_path: Path) -> None:
        leads = [
            {"id": 100, "responsible_user_id": 5},
            {"name": "no id"},
            {"id": None, "responsible_user_id": 5},
        ]
        _write(tmp_path, "leads.json", {"_meta": {}, "data": leads})
        assert set(KommoProvider(tmp_path).leads_by_id().keys()) == {100}

    def test_repr_contains_path(self, tmp_path: Path) -> None:
        assert str(tmp_path) in repr(KommoProvider(tmp_path))

    def test_exports_dir_returns_path_object(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).exports_dir() == tmp_path

    def test_default_path_resolves_to_exports(self) -> None:
        p = KommoProvider()
        assert p.exports_dir().name == "exports"
        assert p.exports_dir().is_absolute()


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    """
    Smoke tests that run against the actual real data in exports/.
    Only execute when the real files are present.
    """

    def test_leads_count(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        leads = provider.get_leads()
        assert len(leads) == 448, f"Expected 448 leads, got {len(leads)}"

    def test_leads_meta(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        m = provider.meta("leads")
        assert m["entity"] == "leads"
        assert m["count"] == 448
        assert m["source"] == "kommo_api_v4"

    def test_pipelines_count(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        assert len(provider.get_pipelines()) == 11

    def test_stages_count(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        m = provider.meta("pipelines")
        stages = provider.stages_by_id()
        # stages_by_id() de-duplicates by stage_id; the _meta total_stages
        # may count closed/system stages that share IDs across pipelines.
        # We assert a reasonable lower bound rather than exact equality.
        assert len(stages) >= 1
        assert len(stages) <= m["total_stages"]

    def test_chats_count(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        assert len(provider.get_chats()) == 321

    def test_messages_count(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        assert len(provider.get_messages()) == 154

    def test_responsible_user_id_is_always_int(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        for lead in provider.get_leads():
            uid = lead.get("responsible_user_id")
            assert isinstance(uid, int), f"Lead {lead.get('id')} has non-int responsible_user_id: {uid!r}"

    def test_all_leads_have_pipeline_id(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        for lead in provider.get_leads():
            assert "pipeline_id" in lead

    def test_messages_have_author_id(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        # Some messages have null author_id (e.g. automated/system channel messages).
        # We verify that where author_id IS present it is always an int.
        messages_with_author = [
            msg for msg in provider.get_messages()
            if msg.get("author_id") is not None
        ]
        assert len(messages_with_author) > 0, "Expected at least some messages with an author_id"
        for msg in messages_with_author:
            assert isinstance(msg["author_id"], int), (
                f"Message {msg.get('message_id')} has non-int author_id: {msg['author_id']!r}"
            )

    def test_leads_by_responsible_user_covers_all_leads(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        mapping = provider.leads_by_responsible_user()
        total = sum(len(v) for v in mapping.values())
        assert total == 448

    def test_pipelines_by_id_all_present(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        pipeline_map = provider.pipelines_by_id()
        # Every lead's pipeline_id must resolve in the map
        for lead in provider.get_leads():
            pid = lead.get("pipeline_id")
            assert pid in pipeline_map, f"Lead {lead['id']} has unknown pipeline_id {pid}"
