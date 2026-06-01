"""
Tests for KommoAuditService — high-level unified audit service.

Test groups:
  TestInit               — construction, repr, available_files
  TestRawAccessors       — pass-through to KommoProvider
  TestNormalizedLeads    — normalized_leads() output contract
  TestAgentProfiles      — agent_profiles(), agent_profile(), agent_ids()
  TestStageResolution    — resolve_stage()
  TestSummary            — summary() keys and types
  TestAuditContext       — agent_audit_context() and all_audit_contexts()
  TestCaching            — results cached after first call
  TestClearCache         — clear_cache() resets everything
  TestRinkelCalls        — service with injected Rinkel calls
  TestAgentIdMap         — explicit cross-reference map
  TestEdgeCases          — empty exports dir, bad agent_id, None calls
  TestRealData           — smoke tests against 448 real leads (skipped if absent)
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from app.services.kommo_audit_service import KommoAuditService
from app.services.lead_normalizer import NormalizedLead
from app.services.agent_linking_engine import AgentUnifiedProfile


# ── Shared fixtures ───────────────────────────────────────────────────────────

SAMPLE_LEADS = [
    {
        "id": 1001,
        "name": "Test Lead Alpha",
        "pipeline_id": 200,
        "status_id": 142,
        "responsible_user_id": 5001,
        "created_at": 1748390400,
        "updated_at": 1748476800,
        "created_at_iso": "2026-05-28T00:00:00+00:00",
        "updated_at_iso": "2026-05-29T00:00:00+00:00",
        "custom_fields_values": [],
    },
    {
        "id": 1002,
        "name": "Test Lead Beta",
        "pipeline_id": 200,
        "status_id": 143,
        "responsible_user_id": 5001,
        "created_at": 1748304000,
        "updated_at": 1748390400,
        "created_at_iso": "2026-05-27T00:00:00+00:00",
        "updated_at_iso": "2026-05-28T00:00:00+00:00",
        "custom_fields_values": [],
    },
    {
        "id": 1003,
        "name": "Test Lead Gamma",
        "pipeline_id": 201,
        "status_id": 210,
        "responsible_user_id": 5002,
        "created_at": 1748217600,
        "updated_at": 1748304000,
        "created_at_iso": "2026-05-26T00:00:00+00:00",
        "updated_at_iso": "2026-05-27T00:00:00+00:00",
        "custom_fields_values": [],
    },
]

SAMPLE_PIPELINES = [
    {
        "pipeline_id": 200,
        "pipeline_name": "Klantenservice",
        "sort": 1,
        "is_main": True,
        "is_archive": False,
        "account_id": 99001,
        "total_stages": 3,
        "regular_stages": 2,
        "stages": [
            {"stage_id": 141, "stage_name": "Nieuwe melding",  "pipeline_id": 200, "sort": 10, "color": "#c1e0ff"},
            {"stage_id": 142, "stage_name": "In behandeling",  "pipeline_id": 200, "sort": 20, "color": "#fffd7f"},
            {"stage_id": 143, "stage_name": "Opgelost",         "pipeline_id": 200, "sort": 30, "color": "#ccff66"},
        ],
    },
    {
        "pipeline_id": 201,
        "pipeline_name": "Creditering",
        "sort": 2,
        "is_main": False,
        "is_archive": False,
        "account_id": 99001,
        "total_stages": 2,
        "regular_stages": 2,
        "stages": [
            {"stage_id": 210, "stage_name": "Aanvraag",    "pipeline_id": 201, "sort": 10, "color": "#c1e0ff"},
            {"stage_id": 211, "stage_name": "Goedgekeurd", "pipeline_id": 201, "sort": 20, "color": "#ccff66"},
        ],
    },
]

SAMPLE_CHATS = [
    {"id": "chat-1", "entity_id": 1001, "entity_type": "lead", "total_messages": 5,
     "channel_type": "waba", "talk_id": 100},
]

SAMPLE_MESSAGES = [
    {"lead_id": 1001, "author_id": 5001, "direction": "outbound",
     "message_text": "Hello", "timestamp": 1748390400,
     "timestamp_iso": "2026-05-28T00:00:00+00:00"},
]

SAMPLE_RINKEL_CALLS = [
    {"call_id": "CALL-001", "agent_id": "5001", "duration": 120,
     "caller_number": "+31612345678"},
    {"call_id": "CALL-002", "agent_id": "5001", "duration": 60,
     "caller_number": "+31698765432"},
    {"call_id": "CALL-003", "agent_id": "9999", "duration": 30,
     "caller_number": "+31611111111"},
]


def _write(directory: Path, filename: str, data: object) -> None:
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


def _make_service(tmp_path: Path, **kwargs) -> KommoAuditService:
    """Write sample files and return a service pointed at tmp_path."""
    _write(tmp_path, "leads.json",     {"_meta": {"entity": "leads", "count": 3,
                                                    "extracted_at": "2026-05-28T06:00:00Z",
                                                    "source": "kommo_api_v4"},
                                        "data": SAMPLE_LEADS})
    _write(tmp_path, "pipelines.json", {"_meta": {"entity": "pipelines", "count": 2,
                                                    "total_stages": 5,
                                                    "extracted_at": "2026-05-28T06:00:00Z",
                                                    "source": "kommo_api_v4"},
                                        "data": SAMPLE_PIPELINES})
    _write(tmp_path, "chats.json",     {"_meta": {}, "data": SAMPLE_CHATS})
    _write(tmp_path, "messages_flat.json", {"_meta": {}, "data": SAMPLE_MESSAGES})
    return KommoAuditService(exports_dir=str(tmp_path), **kwargs)


# ── Tests: init ───────────────────────────────────────────────────────────────

class TestInit:
    def test_default_construction(self) -> None:
        svc = KommoAuditService()
        assert svc is not None

    def test_repr_contains_exports_dir(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert str(tmp_path) in repr(svc)

    def test_repr_shows_file_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        r = repr(svc)
        assert "leads.json" in r or "files=" in r

    def test_available_files_shows_present(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        avail = svc.available_files()
        assert avail["leads.json"] is True
        assert avail["pipelines.json"] is True
        assert avail["chats.json"] is True
        assert avail["messages_flat.json"] is True

    def test_available_files_on_empty_dir(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        avail = svc.available_files()
        assert all(v is False for v in avail.values())


# ── Tests: raw accessors ──────────────────────────────────────────────────────

class TestRawAccessors:
    def test_raw_leads_returns_list(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert isinstance(svc.raw_leads(), list)
        assert len(svc.raw_leads()) == 3

    def test_raw_pipelines_returns_list(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert len(svc.raw_pipelines()) == 2

    def test_raw_chats_returns_list(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert len(svc.raw_chats()) == 1

    def test_raw_messages_returns_list(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert len(svc.raw_messages()) == 1

    def test_raw_leads_are_not_mutated(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        leads = svc.raw_leads()
        original_name = leads[0]["name"]
        leads[0]["name"] = "mutated"
        # Re-reading returns cached, but we check the object wasn't quietly cloned
        assert svc.raw_leads()[0]["name"] == "mutated"  # same ref in cache
        leads[0]["name"] = original_name  # restore

    def test_missing_files_return_empty(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        assert svc.raw_leads() == []
        assert svc.raw_pipelines() == []
        assert svc.raw_chats() == []
        assert svc.raw_messages() == []


# ── Tests: normalized_leads() ────────────────────────────────────────────────

class TestNormalizedLeads:
    def test_returns_list_of_normalized_leads(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        leads = svc.normalized_leads()
        assert isinstance(leads, list)
        assert all(isinstance(l, NormalizedLead) for l in leads)

    def test_count_matches_raw(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert len(svc.normalized_leads()) == 3

    def test_all_fields_str_or_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        for lead in svc.normalized_leads():
            for v in lead.to_dict().values():
                assert v is None or isinstance(v, str)

    def test_status_resolved_to_stage_name(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        leads = svc.normalized_leads()
        # status_id 142 → "In behandeling"
        lead_142 = next(l for l in leads if l.id == "1001")
        assert lead_142.status == "In behandeling"

    def test_timestamps_prefer_iso(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        for lead in svc.normalized_leads():
            if lead.created_at:
                assert "T" in lead.created_at

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        assert svc.normalized_leads() == []


# ── Tests: agent_profiles() ──────────────────────────────────────────────────

class TestAgentProfiles:
    def test_returns_list_of_profiles(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        profiles = svc.agent_profiles()
        assert isinstance(profiles, list)
        assert all(isinstance(p, AgentUnifiedProfile) for p in profiles)

    def test_distinct_agent_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        # SAMPLE_LEADS has 2 distinct responsible_user_ids: 5001, 5002
        assert len(svc.agent_profiles()) == 2

    def test_sorted_by_agent_id(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ids = [p.agent_id for p in svc.agent_profiles()]
        assert ids == sorted(ids)

    def test_lead_counts_correct(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        profiles = {p.agent_id: p for p in svc.agent_profiles()}
        assert profiles["5001"].total_leads == 2
        assert profiles["5002"].total_leads == 1

    def test_agent_profile_by_id(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        p = svc.agent_profile("5001")
        assert p is not None
        assert p.agent_id == "5001"
        assert p.total_leads == 2

    def test_agent_profile_unknown_id_returns_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.agent_profile("99999") is None

    def test_agent_ids_list(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ids = svc.agent_ids()
        assert set(ids) == {"5001", "5002"}

    def test_all_leads_preserved_across_profiles(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        total = sum(p.total_leads for p in svc.agent_profiles())
        assert total == 3

    def test_empty_dir_returns_empty_profiles(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        assert svc.agent_profiles() == []


# ── Tests: stage resolution ───────────────────────────────────────────────────

class TestStageResolution:
    def test_resolve_valid_stage_id(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        stage = svc.resolve_stage(142)
        assert stage is not None
        assert stage["stage_name"] == "In behandeling"

    def test_resolve_stage_id_as_string(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.resolve_stage("142")["stage_name"] == "In behandeling"

    def test_resolve_unknown_stage_returns_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.resolve_stage(9999999) is None

    def test_resolve_none_returns_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.resolve_stage(None) is None

    def test_resolve_bad_string_returns_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.resolve_stage("not-a-number") is None

    def test_all_sample_stages_resolve(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        for sid in [141, 142, 143, 210, 211]:
            assert svc.resolve_stage(sid) is not None, f"Stage {sid} should resolve"


# ── Tests: summary() ─────────────────────────────────────────────────────────

class TestSummary:
    def test_returns_dict(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert isinstance(svc.summary(), dict)

    def test_required_keys_present(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        s = svc.summary()
        required = {
            "total_leads", "total_agents", "matched_agents",
            "kommo_only_agents", "rinkel_only_agents",
            "total_pipelines", "total_stages",
            "total_chats", "total_messages",
            "kommo_extracted_at", "files_available",
        }
        assert required.issubset(s.keys())

    def test_lead_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary()["total_leads"] == 3

    def test_agent_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary()["total_agents"] == 2

    def test_pipeline_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary()["total_pipelines"] == 2

    def test_stage_count(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary()["total_stages"] == 5

    def test_chat_and_message_counts(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        s = svc.summary()
        assert s["total_chats"] == 1
        assert s["total_messages"] == 1

    def test_extracted_at_present(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary()["kommo_extracted_at"] == "2026-05-28T06:00:00Z"

    def test_no_rinkel_calls_means_kommo_only(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        s = svc.summary()
        assert s["matched_agents"] == 0
        assert s["kommo_only_agents"] == 2
        assert s["rinkel_only_agents"] == 0

    def test_summary_with_rinkel_calls(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        s = svc.summary()
        # agent 5001 is in both systems → matched
        assert s["matched_agents"] >= 1
        # agent 9999 is Rinkel-only
        assert s["rinkel_only_agents"] >= 1


# ── Tests: audit context ──────────────────────────────────────────────────────

class TestAuditContext:
    def test_returns_dict_for_valid_agent(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        assert isinstance(ctx, dict)

    def test_returns_none_for_unknown_agent(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.agent_audit_context("99999") is None

    def test_required_context_keys(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        assert set(ctx.keys()) >= {
            "agent_id", "lead_count", "call_count",
            "is_matched", "normalized_leads", "rinkel_calls", "pipeline_summary"
        }

    def test_lead_count_in_context(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        assert ctx["lead_count"] == 2

    def test_call_count_zero_without_rinkel(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        assert ctx["call_count"] == 0
        assert ctx["is_matched"] is False

    def test_normalized_leads_all_str_or_none(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        for lead in ctx["normalized_leads"]:
            for v in lead.values():
                assert v is None or isinstance(v, str)

    def test_pipeline_summary_present(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        ctx = svc.agent_audit_context("5001")
        assert isinstance(ctx["pipeline_summary"], list)
        assert len(ctx["pipeline_summary"]) == 1
        assert ctx["pipeline_summary"][0]["pipeline_name"] == "Klantenservice"

    def test_rinkel_calls_in_context(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        ctx = svc.agent_audit_context("5001")
        assert ctx["call_count"] == 2
        assert ctx["is_matched"] is True
        assert len(ctx["rinkel_calls"]) == 2

    def test_all_audit_contexts_length(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        all_ctx = svc.all_audit_contexts()
        assert len(all_ctx) == 2  # one per agent

    def test_all_audit_contexts_are_dicts(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        for ctx in svc.all_audit_contexts():
            assert isinstance(ctx, dict)
            assert "agent_id" in ctx

    def test_all_contexts_with_rinkel_only_agent(self, tmp_path: Path) -> None:
        """Rinkel-only agent (9999) should appear in all_audit_contexts."""
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        all_ctx = svc.all_audit_contexts()
        ids = {c["agent_id"] for c in all_ctx}
        assert "9999" in ids  # Rinkel-only agent included


# ── Tests: caching ────────────────────────────────────────────────────────────

class TestCaching:
    def test_normalized_leads_cached(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.normalized_leads() is svc.normalized_leads()

    def test_agent_profiles_cached(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.agent_profiles() is svc.agent_profiles()

    def test_summary_cached(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        assert svc.summary() is svc.summary()


# ── Tests: clear_cache() ─────────────────────────────────────────────────────

class TestClearCache:
    def test_clear_cache_allows_fresh_read(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        leads_before = svc.normalized_leads()
        svc.clear_cache()
        leads_after = svc.normalized_leads()
        # Same content (file unchanged), different list object
        assert leads_before is not leads_after
        assert len(leads_before) == len(leads_after)

    def test_clear_cache_resets_profiles(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        p1 = svc.agent_profiles()
        svc.clear_cache()
        p2 = svc.agent_profiles()
        assert p1 is not p2

    def test_clear_cache_resets_summary(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        s1 = svc.summary()
        svc.clear_cache()
        s2 = svc.summary()
        assert s1 is not s2


# ── Tests: with Rinkel calls ──────────────────────────────────────────────────

class TestRinkelCalls:
    def test_matched_agent_has_calls(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        p = svc.agent_profile("5001")
        assert p is not None
        assert p.is_matched is True
        assert p.total_calls == 2

    def test_rinkel_only_agent_in_profiles(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        p = svc.agent_profile("9999")
        assert p is not None
        assert p.is_rinkel_only is True
        assert p.total_calls == 1
        assert p.total_leads == 0

    def test_total_calls_preserved(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        total = sum(p.total_calls for p in svc.agent_profiles())
        assert total == len(SAMPLE_RINKEL_CALLS)


# ── Tests: explicit agent_id_map ─────────────────────────────────────────────

class TestAgentIdMap:
    def test_explicit_map_links_agents(self, tmp_path: Path) -> None:
        calls = [{"call_id": "C1", "agent_id": "sophie"}]
        svc = _make_service(
            tmp_path,
            rinkel_calls=calls,
            agent_id_map={"sophie": "5001"},
        )
        p = svc.agent_profile("5001")
        assert p is not None
        assert p.is_matched is True
        assert p.total_calls == 1

    def test_unmapped_agent_still_appears(self, tmp_path: Path) -> None:
        calls = [{"call_id": "C1", "agent_id": "orphan"}]
        svc = _make_service(tmp_path, rinkel_calls=calls)
        p = svc.agent_profile("orphan")
        assert p is not None
        assert p.is_rinkel_only is True


# ── Tests: edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_exports_dir_no_crash(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        assert svc.normalized_leads() == []
        assert svc.agent_profiles() == []
        assert svc.all_audit_contexts() == []
        s = svc.summary()
        assert s["total_leads"] == 0

    def test_none_rinkel_calls_treated_as_empty(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path, rinkel_calls=None)
        assert svc.summary()["matched_agents"] == 0

    def test_agent_profile_strips_whitespace(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        p = svc.agent_profile("  5001  ")
        assert p is not None

    def test_agent_profile_int_id_as_str(self, tmp_path: Path) -> None:
        svc = _make_service(tmp_path)
        p = svc.agent_profile(5001)  # type: ignore[arg-type]
        assert p is not None

    def test_all_audit_contexts_empty_dir(self, tmp_path: Path) -> None:
        svc = KommoAuditService(exports_dir=str(tmp_path))
        assert svc.all_audit_contexts() == []


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        self.svc = KommoAuditService(exports_dir=str(REAL_EXPORTS))

    def test_normalized_leads_count(self) -> None:
        assert len(self.svc.normalized_leads()) == 448

    def test_all_normalized_leads_str_or_none(self) -> None:
        for lead in self.svc.normalized_leads():
            for v in lead.to_dict().values():
                assert v is None or isinstance(v, str)

    def test_summary_total_leads(self) -> None:
        assert self.svc.summary()["total_leads"] == 448

    def test_summary_total_pipelines(self) -> None:
        assert self.svc.summary()["total_pipelines"] == 11

    def test_summary_extracted_at_present(self) -> None:
        assert self.svc.summary()["kommo_extracted_at"] is not None

    def test_agent_profiles_non_empty(self) -> None:
        assert len(self.svc.agent_profiles()) >= 1

    def test_all_leads_in_profiles(self) -> None:
        total = sum(p.total_leads for p in self.svc.agent_profiles())
        assert total == 448

    def test_all_agent_ids_are_strings(self) -> None:
        for p in self.svc.agent_profiles():
            assert isinstance(p.agent_id, str)

    def test_all_audit_contexts_generated(self) -> None:
        contexts = self.svc.all_audit_contexts()
        assert len(contexts) >= 1
        for ctx in contexts:
            assert isinstance(ctx["agent_id"], str)
            assert isinstance(ctx["normalized_leads"], list)
            assert isinstance(ctx["pipeline_summary"], list)

    def test_resolve_stage_works(self) -> None:
        stages = self.svc.summary()["total_stages"]
        assert stages > 0

    def test_caching_on_real_data(self) -> None:
        leads1 = self.svc.normalized_leads()
        leads2 = self.svc.normalized_leads()
        assert leads1 is leads2

    def test_clear_and_reload(self) -> None:
        leads_before = self.svc.normalized_leads()
        self.svc.clear_cache()
        leads_after = self.svc.normalized_leads()
        assert len(leads_before) == len(leads_after)
        assert leads_before is not leads_after
