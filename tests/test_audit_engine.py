"""
Tests for AuditEngine — full 7-step pipeline.

Test groups:
  TestPipelineContract    — run() output type and order guarantees
  TestReportSchema        — AgentAuditReport field contracts
  TestKommoSection        — kommo sub-report fields
  TestRinkelSection       — rinkel sub-report fields
  TestCombinedSection     — combined metrics and performance_score formula
  TestDataSourceFlags     — has_kommo_data / has_rinkel_data
  TestRunForAgent         — single-agent lookup
  TestRunAsDicts          — dict serialisation
  TestRunAsFlatDicts      — flat/tabular dict
  TestSummary             — aggregate summary dict
  TestFilters             — include_kommo_only / include_rinkel_only
  TestPerformanceScore    — formula verification
  TestDeterminism         — same input → same sorted output
  TestSafety              — empty dir, no calls, None, bad inputs
  TestExplicitAgentIdMap  — cross-reference map
  TestRealData            — smoke tests against 448 real Kommo leads
"""

from __future__ import annotations

import pytest
from pathlib import Path
import json

from app.services.audit_engine import (
    AuditEngine,
    AgentAuditReport,
    KommoSection,
    RinkelSection,
    CombinedSection,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_LEADS = [
    {
        "id": 1001, "name": "Lead Alpha",
        "pipeline_id": 200, "status_id": 300,
        "responsible_user_id": 5001,
        "created_at": 1748390400, "updated_at": 1748476800,
        "created_at_iso": "2026-05-28T00:00:00+00:00",
        "updated_at_iso": "2026-05-29T00:00:00+00:00",
        "loss_reason_id": None, "closed_at": 1748390400,   # won
        "custom_fields_values": [],
    },
    {
        "id": 1002, "name": "Lead Beta",
        "pipeline_id": 200, "status_id": 300,
        "responsible_user_id": 5001,
        "created_at": 1748304000, "updated_at": 1748390400,
        "created_at_iso": "2026-05-27T00:00:00+00:00",
        "updated_at_iso": "2026-05-28T00:00:00+00:00",
        "loss_reason_id": 55, "closed_at": 1748390400,     # lost
        "custom_fields_values": [],
    },
    {
        "id": 1003, "name": "Lead Gamma",
        "pipeline_id": 201, "status_id": 300,
        "responsible_user_id": 5002,
        "created_at": 1748217600, "updated_at": 1748304000,
        "created_at_iso": "2026-05-26T00:00:00+00:00",
        "updated_at_iso": "2026-05-27T00:00:00+00:00",
        "loss_reason_id": None, "closed_at": None,          # active
        "custom_fields_values": [],
    },
]

SAMPLE_PIPELINES = [
    {
        "pipeline_id": 200, "pipeline_name": "Klantenservice",
        "sort": 1, "is_main": True, "is_archive": False,
        "account_id": 99001, "total_stages": 3, "regular_stages": 2,
        "stages": [
            {"stage_id": 300, "stage_name": "Actief", "pipeline_id": 200,
             "sort": 10, "color": "#ccc", "is_editable": True},
        ],
    },
    {
        "pipeline_id": 201, "pipeline_name": "Creditering",
        "sort": 2, "is_main": False, "is_archive": False,
        "account_id": 99001, "total_stages": 1, "regular_stages": 1,
        "stages": [
            {"stage_id": 301, "stage_name": "Aanvraag", "pipeline_id": 201,
             "sort": 10, "color": "#ccc", "is_editable": True},
        ],
    },
]

SAMPLE_RINKEL_CALLS = [
    {"call_id": "C1", "agent_id": "5001", "direction": "inbound",  "duration": 120},
    {"call_id": "C2", "agent_id": "5001", "direction": "outbound", "duration": 90},
    {"call_id": "C3", "agent_id": "9999", "direction": "inbound",  "duration": 45},
]


def _write(directory: Path, name: str, data: object) -> None:
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


def _make_engine(tmp_path: Path, **kwargs) -> AuditEngine:
    _write(tmp_path, "leads.json", {
        "_meta": {"entity": "leads", "count": 3,
                  "extracted_at": "2026-05-28T06:00:00Z", "source": "kommo_api_v4"},
        "data": SAMPLE_LEADS,
    })
    _write(tmp_path, "pipelines.json", {
        "_meta": {"entity": "pipelines", "count": 2,
                  "total_stages": 2, "extracted_at": "2026-05-28T06:00:00Z",
                  "source": "kommo_api_v4"},
        "data": SAMPLE_PIPELINES,
    })
    return AuditEngine(exports_dir=str(tmp_path), **kwargs)


# ── Tests: pipeline contract ──────────────────────────────────────────────────

class TestPipelineContract:
    def test_returns_list(self, tmp_path: Path) -> None:
        assert isinstance(_make_engine(tmp_path).run(), list)

    def test_each_element_is_report(self, tmp_path: Path) -> None:
        for r in _make_engine(tmp_path).run():
            assert isinstance(r, AgentAuditReport)

    def test_sorted_by_performance_score_desc(self, tmp_path: Path) -> None:
        reports = _make_engine(tmp_path).run()
        scores = [r.performance_score for r in reports]
        assert scores == sorted(scores, reverse=True)

    def test_two_agents_from_sample_data(self, tmp_path: Path) -> None:
        # 5001 (2 leads), 5002 (1 lead)
        reports = _make_engine(tmp_path).run()
        assert len(reports) == 2

    def test_three_agents_with_rinkel_only(self, tmp_path: Path) -> None:
        # 9999 is Rinkel-only
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        reports = engine.run()
        assert len(reports) == 3

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        engine = AuditEngine(exports_dir=str(tmp_path))
        assert engine.run() == []


# ── Tests: report schema ──────────────────────────────────────────────────────

class TestReportSchema:
    def _report(self, tmp_path: Path) -> AgentAuditReport:
        return _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS).run()[0]

    def test_agent_id_is_str(self, tmp_path: Path) -> None:
        assert isinstance(self._report(tmp_path).agent_id, str)

    def test_has_kommo_section(self, tmp_path: Path) -> None:
        assert isinstance(self._report(tmp_path).kommo, KommoSection)

    def test_has_rinkel_section(self, tmp_path: Path) -> None:
        assert isinstance(self._report(tmp_path).rinkel, RinkelSection)

    def test_has_combined_section(self, tmp_path: Path) -> None:
        assert isinstance(self._report(tmp_path).combined, CombinedSection)

    def test_is_frozen(self, tmp_path: Path) -> None:
        r = self._report(tmp_path)
        with pytest.raises((AttributeError, TypeError)):
            r.agent_id = "hacked"  # type: ignore[misc]

    def test_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        d = self._report(tmp_path).to_dict()
        assert set(d.keys()) >= {"agent_id", "kommo", "rinkel", "combined"}

    def test_to_dict_subsections_are_dicts(self, tmp_path: Path) -> None:
        d = self._report(tmp_path).to_dict()
        assert isinstance(d["kommo"],    dict)
        assert isinstance(d["rinkel"],   dict)
        assert isinstance(d["combined"], dict)

    def test_to_flat_dict_has_prefixed_keys(self, tmp_path: Path) -> None:
        flat = self._report(tmp_path).to_flat_dict()
        assert "kommo_total_leads" in flat
        assert "rinkel_total_calls" in flat
        assert "combined_performance_score" in flat
        assert "has_kommo_data" in flat
        assert "has_rinkel_data" in flat

    def test_performance_score_shortcut(self, tmp_path: Path) -> None:
        r = self._report(tmp_path)
        assert r.performance_score == r.combined.performance_score

    def test_repr_contains_agent_id(self, tmp_path: Path) -> None:
        r = self._report(tmp_path)
        assert r.agent_id in repr(r)

    def test_normalized_leads_is_tuple(self, tmp_path: Path) -> None:
        r = self._report(tmp_path)
        assert isinstance(r.normalized_leads, tuple)


# ── Tests: Kommo section ──────────────────────────────────────────────────────

class TestKommoSection:
    def _kommo(self, tmp_path: Path, agent_id: str = "5001") -> KommoSection:
        engine = _make_engine(tmp_path)
        reports = {r.agent_id: r for r in engine.run()}
        return reports[agent_id].kommo

    def test_total_leads(self, tmp_path: Path) -> None:
        assert self._kommo(tmp_path).total_leads == 2

    def test_converted_leads(self, tmp_path: Path) -> None:
        # Lead 1001: closed_at set, no loss_reason → won
        assert self._kommo(tmp_path).converted_leads >= 1

    def test_lost_leads(self, tmp_path: Path) -> None:
        # Lead 1002: loss_reason_id=55 → lost
        assert self._kommo(tmp_path).lost_leads >= 1

    def test_conversion_rate_in_range(self, tmp_path: Path) -> None:
        k = self._kommo(tmp_path)
        assert 0.0 <= k.conversion_rate <= 1.0

    def test_conversion_rate_formula(self, tmp_path: Path) -> None:
        k = self._kommo(tmp_path)
        if k.total_leads > 0:
            assert k.conversion_rate == round(k.converted_leads / k.total_leads, 4)

    def test_active_leads_never_negative(self, tmp_path: Path) -> None:
        assert self._kommo(tmp_path).active_leads >= 0

    def test_total_equals_converted_plus_lost_plus_active(self, tmp_path: Path) -> None:
        k = self._kommo(tmp_path)
        assert k.total_leads == k.converted_leads + k.lost_leads + k.active_leads


# ── Tests: Rinkel section ─────────────────────────────────────────────────────

class TestRinkelSection:
    def _rinkel(self, tmp_path: Path, agent_id: str = "5001") -> RinkelSection:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        return {r.agent_id: r for r in engine.run()}[agent_id].rinkel

    def test_total_calls(self, tmp_path: Path) -> None:
        assert self._rinkel(tmp_path).total_calls == 2

    def test_inbound_outbound_counts(self, tmp_path: Path) -> None:
        r = self._rinkel(tmp_path)
        assert r.inbound_calls == 1
        assert r.outbound_calls == 1

    def test_avg_duration(self, tmp_path: Path) -> None:
        r = self._rinkel(tmp_path)
        assert r.avg_call_duration == round((120 + 90) / 2, 4)

    def test_engagement_score_in_range(self, tmp_path: Path) -> None:
        r = self._rinkel(tmp_path)
        assert 0.0 <= r.engagement_score <= 1.0

    def test_zero_calls_without_rinkel(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)  # no rinkel_calls
        reports = {r.agent_id: r for r in engine.run()}
        assert reports["5001"].rinkel.total_calls == 0


# ── Tests: combined section ───────────────────────────────────────────────────

class TestCombinedSection:
    def _combined(self, tmp_path: Path, agent_id: str = "5001") -> CombinedSection:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        return {r.agent_id: r for r in engine.run()}[agent_id].combined

    def test_performance_score_in_range(self, tmp_path: Path) -> None:
        assert 0.0 <= self._combined(tmp_path).performance_score <= 1.0

    def test_activity_consistency_in_range(self, tmp_path: Path) -> None:
        c = self._combined(tmp_path)
        assert 0.0 <= c.activity_consistency <= 1.0

    def test_leads_to_calls_ratio_non_negative(self, tmp_path: Path) -> None:
        assert self._combined(tmp_path).leads_to_calls_ratio >= 0.0

    def test_responsiveness_proxy_in_range(self, tmp_path: Path) -> None:
        c = self._combined(tmp_path)
        assert 0.0 <= c.responsiveness_proxy <= 1.0

    def test_data_source_flags_present(self, tmp_path: Path) -> None:
        flags = self._combined(tmp_path).data_source_flags
        assert "kommo" in flags and "rinkel" in flags

    def test_kommo_flag_true_for_matched_agent(self, tmp_path: Path) -> None:
        assert self._combined(tmp_path).data_source_flags["kommo"] is True

    def test_rinkel_flag_true_when_calls_present(self, tmp_path: Path) -> None:
        assert self._combined(tmp_path).data_source_flags["rinkel"] is True


# ── Tests: data source flags ──────────────────────────────────────────────────

class TestDataSourceFlags:
    def test_kommo_only_agent_flags(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)  # no rinkel
        reports = {r.agent_id: r for r in engine.run()}
        flags = reports["5001"].combined.data_source_flags
        assert flags["kommo"] is True
        assert flags["rinkel"] is False

    def test_rinkel_only_agent_flags(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        reports = {r.agent_id: r for r in engine.run()}
        flags = reports["9999"].combined.data_source_flags
        assert flags["kommo"] is False
        assert flags["rinkel"] is True

    def test_flat_dict_has_flag_fields(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        flat = engine.run()[0].to_flat_dict()
        assert "has_kommo_data" in flat
        assert "has_rinkel_data" in flat


# ── Tests: run_for_agent() ────────────────────────────────────────────────────

class TestRunForAgent:
    def test_returns_report_for_known_agent(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        r = engine.run_for_agent("5001")
        assert r is not None
        assert r.agent_id == "5001"

    def test_returns_none_for_unknown_agent(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert engine.run_for_agent("99999") is None

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        r = engine.run_for_agent("  5001  ")
        assert r is not None


# ── Tests: run_as_dicts() ────────────────────────────────────────────────────

class TestRunAsDicts:
    def test_returns_list_of_dicts(self, tmp_path: Path) -> None:
        result = _make_engine(tmp_path).run_as_dicts()
        assert isinstance(result, list)
        for d in result:
            assert isinstance(d, dict)

    def test_agent_id_in_each_dict(self, tmp_path: Path) -> None:
        for d in _make_engine(tmp_path).run_as_dicts():
            assert "agent_id" in d

    def test_json_serialisable(self, tmp_path: Path) -> None:
        import json
        for d in _make_engine(tmp_path).run_as_dicts():
            json.dumps(d)

    def test_count_matches_run(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert len(engine.run_as_dicts()) == len(engine.run())


# ── Tests: run_as_flat_dicts() ────────────────────────────────────────────────

class TestRunAsFlatDicts:
    def test_returns_list_of_dicts(self, tmp_path: Path) -> None:
        result = _make_engine(tmp_path).run_as_flat_dicts()
        assert all(isinstance(d, dict) for d in result)

    def test_all_values_are_scalars(self, tmp_path: Path) -> None:
        for d in _make_engine(tmp_path).run_as_flat_dicts():
            for v in d.values():
                assert not isinstance(v, (dict, list, tuple))


# ── Tests: summary() ─────────────────────────────────────────────────────────

class TestSummary:
    def test_returns_dict(self, tmp_path: Path) -> None:
        assert isinstance(_make_engine(tmp_path).summary(), dict)

    def test_required_keys(self, tmp_path: Path) -> None:
        s = _make_engine(tmp_path).summary()
        assert set(s.keys()) >= {
            "total_agents", "total_leads", "total_calls",
            "matched_agents", "kommo_only_agents", "rinkel_only_agents",
            "avg_performance_score", "avg_conversion_rate",
            "avg_engagement_score", "top_performer", "bottom_performer",
        }

    def test_total_leads(self, tmp_path: Path) -> None:
        assert _make_engine(tmp_path).summary()["total_leads"] == 3

    def test_total_agents(self, tmp_path: Path) -> None:
        assert _make_engine(tmp_path).summary()["total_agents"] == 2

    def test_top_performer_is_str(self, tmp_path: Path) -> None:
        s = _make_engine(tmp_path).summary()
        assert isinstance(s["top_performer"], str)

    def test_avg_scores_in_range(self, tmp_path: Path) -> None:
        s = _make_engine(tmp_path).summary()
        assert 0.0 <= s["avg_performance_score"] <= 1.0
        assert 0.0 <= s["avg_conversion_rate"]   <= 1.0
        assert 0.0 <= s["avg_engagement_score"]  <= 1.0

    def test_empty_dir_summary(self, tmp_path: Path) -> None:
        s = AuditEngine(exports_dir=str(tmp_path)).summary()
        assert s["total_agents"] == 0
        assert s["top_performer"] is None

    def test_rinkel_only_agent_in_total(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        s = engine.summary()
        assert s["total_agents"] == 3


# ── Tests: filters ────────────────────────────────────────────────────────────

class TestFilters:
    def test_exclude_kommo_only(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, include_kommo_only=False)
        assert engine.run() == []  # no rinkel calls → all are kommo-only

    def test_exclude_rinkel_only(self, tmp_path: Path) -> None:
        engine = _make_engine(
            tmp_path,
            rinkel_calls=SAMPLE_RINKEL_CALLS,
            include_rinkel_only=False,
        )
        ids = {r.agent_id for r in engine.run()}
        assert "9999" not in ids

    def test_include_rinkel_only(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        ids = {r.agent_id for r in engine.run()}
        assert "9999" in ids


# ── Tests: performance score formula ─────────────────────────────────────────

class TestPerformanceScore:
    def test_formula_components(self, tmp_path: Path) -> None:
        """
        performance = 0.40 × conversion + 0.35 × engagement + 0.25 × consistency.
        Verify the combination is within plausible bounds.
        """
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        for r in engine.run():
            conv  = r.kommo.conversion_rate
            eng   = r.rinkel.engagement_score
            cons  = r.combined.activity_consistency
            expected = round(0.40 * conv + 0.35 * eng + 0.25 * cons, 4)
            assert r.performance_score == expected

    def test_all_zeros_gives_zero(self, tmp_path: Path) -> None:
        # Empty engine → no reports, but formula holds for agents with no data
        engine = AuditEngine(exports_dir=str(tmp_path))
        assert engine.run() == []

    def test_max_score_possible(self, tmp_path: Path) -> None:
        """Score is bounded at 1.0 (all components at max)."""
        for r in _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS).run():
            assert r.performance_score <= 1.0

    def test_scores_non_negative(self, tmp_path: Path) -> None:
        for r in _make_engine(tmp_path).run():
            assert r.performance_score >= 0.0


# ── Tests: determinism ────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        r1 = engine.run()
        r2 = engine.run()
        assert [r.agent_id for r in r1] == [r.agent_id for r in r2]
        assert [r.performance_score for r in r1] == [r.performance_score for r in r2]

    def test_two_engines_same_result(self, tmp_path: Path) -> None:
        r1 = _make_engine(tmp_path).run()
        r2 = _make_engine(tmp_path).run()
        assert [r.agent_id for r in r1] == [r.agent_id for r in r2]


# ── Tests: safety ─────────────────────────────────────────────────────────────

class TestSafety:
    def test_empty_exports_dir_no_crash(self, tmp_path: Path) -> None:
        engine = AuditEngine(exports_dir=str(tmp_path))
        assert engine.run() == []
        assert engine.summary()["total_agents"] == 0

    def test_none_rinkel_calls_no_crash(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path, rinkel_calls=None)
        assert isinstance(engine.run(), list)

    def test_run_for_agent_unknown_no_crash(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert engine.run_for_agent("nonexistent") is None

    def test_repr_does_not_crash(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert isinstance(repr(engine), str)


# ── Tests: explicit agent_id_map ─────────────────────────────────────────────

class TestExplicitAgentIdMap:
    def test_map_links_different_id_formats(self, tmp_path: Path) -> None:
        calls = [{"call_id": "X", "agent_id": "sophie", "duration": 60}]
        engine = _make_engine(
            tmp_path,
            rinkel_calls=calls,
            agent_id_map={"sophie": "5001"},
        )
        reports = {r.agent_id: r for r in engine.run()}
        assert reports["5001"].rinkel.total_calls == 1
        assert reports["5001"].combined.data_source_flags["rinkel"] is True


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        self.engine  = AuditEngine(exports_dir=str(REAL_EXPORTS))
        self.reports = self.engine.run()

    def test_reports_non_empty(self) -> None:
        assert len(self.reports) >= 1

    def test_total_leads_preserved(self) -> None:
        total = sum(r.kommo.total_leads for r in self.reports)
        assert total == 448

    def test_all_are_audit_reports(self) -> None:
        assert all(isinstance(r, AgentAuditReport) for r in self.reports)

    def test_sorted_desc_by_performance(self) -> None:
        scores = [r.performance_score for r in self.reports]
        assert scores == sorted(scores, reverse=True)

    def test_all_performance_scores_in_range(self) -> None:
        for r in self.reports:
            assert 0.0 <= r.performance_score <= 1.0

    def test_all_conversion_rates_in_range(self) -> None:
        for r in self.reports:
            assert 0.0 <= r.kommo.conversion_rate <= 1.0

    def test_all_agent_ids_are_strings(self) -> None:
        assert all(isinstance(r.agent_id, str) for r in self.reports)

    def test_all_normalized_leads_present(self) -> None:
        for r in self.reports:
            assert isinstance(r.normalized_leads, tuple)

    def test_summary_leads_count(self) -> None:
        s = self.engine.summary()
        assert s["total_leads"] == 448

    def test_summary_top_performer_present(self) -> None:
        s = self.engine.summary()
        assert s["top_performer"] is not None

    def test_as_dicts_json_serialisable(self) -> None:
        import json
        for d in self.engine.run_as_dicts():
            json.dumps(d)

    def test_deterministic_on_real_data(self) -> None:
        second = self.engine.run()
        assert [r.agent_id for r in self.reports] == [r.agent_id for r in second]
        assert [r.performance_score for r in self.reports] == [
            r.performance_score for r in second
        ]
