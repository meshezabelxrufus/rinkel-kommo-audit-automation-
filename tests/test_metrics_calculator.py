"""
Tests for MetricsCalculator.

Rules under test:
  - Never raises on any input (bad profile, None, non-dict leads/calls)
  - All float fields are 0.0–1.0 where applicable (engagement, rates)
  - conversion_rate = converted / total_leads (0.0 when no leads)
  - lost/won classification via loss_reason_id, closed_at, stage keywords
  - engagement_score = 0.4*conversion + 0.4*min(calls/leads,1) + 0.2*consistency
  - Deterministic: same input → same output
  - All floats rounded to 4 decimal places
  - calculate_many() preserves order and handles bad records

Test groups:
  TestAgentMetricsContract   — output type and field guarantees
  TestLeadClassification     — won / lost / active classification logic
  TestKommoMetrics           — conversion_rate, counts
  TestRinkelMetrics          — total_calls, avg_duration, inbound/outbound
  TestCrossMetrics           — ratio, coverage, consistency, engagement
  TestEngagementScore        — formula verification
  TestSafety                 — None, non-profile, empty, all-zero inputs
  TestCalculateMany          — batch method contract
  TestDeterminism            — identical inputs → identical outputs
  TestStageMap               — stage_map classification (keyword + editable)
  TestExplicitStageIds       — won_stage_ids / lost_stage_ids overrides
  TestToDict                 — serialisation
  TestRealData               — smoke tests against 448 real Kommo leads
"""

from __future__ import annotations

import pytest
from pathlib import Path

from app.services.metrics_calculator import MetricsCalculator, AgentMetrics
from app.services.agent_linking_engine import AgentUnifiedProfile
from app.integrations.kommo import KommoProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_lead(
    *,
    status_id: int = 100,
    loss_reason_id=None,
    closed_at=None,
    responsible_user_id: int = 1,
    **kw,
) -> dict:
    return {
        "id": kw.pop("id", 1),
        "responsible_user_id": responsible_user_id,
        "status_id": status_id,
        "loss_reason_id": loss_reason_id,
        "closed_at": closed_at,
        "pipeline_id": 10,
        **kw,
    }


def make_call(
    *,
    direction: str = "inbound",
    duration: int = 120,
    agent_id: str = "1",
    **kw,
) -> dict:
    return {
        "call_id": kw.pop("call_id", "C1"),
        "agent_id": agent_id,
        "direction": direction,
        "duration": duration,
        **kw,
    }


def make_profile(
    agent_id: str = "1",
    leads: list | None = None,
    calls: list | None = None,
) -> AgentUnifiedProfile:
    return AgentUnifiedProfile(
        agent_id=agent_id,
        kommo_leads=leads or [],
        rinkel_calls=calls or [],
    )


# Stage map reflecting real Kommo data structure
STAGE_MAP = {
    # Non-editable terminal stages
    142: {"stage_id": 142, "stage_name": "Leads ganados", "is_editable": False},   # WON
    143: {"stage_id": 143, "stage_name": "Leads perdidos", "is_editable": False},  # LOST
    200: {"stage_id": 200, "stage_name": "Incoming leads", "is_editable": False},  # neutral
    # Editable (active) stages
    300: {"stage_id": 300, "stage_name": "Frios",    "is_editable": True},
    301: {"stage_id": 301, "stage_name": "Llamada",  "is_editable": True},
    302: {"stage_id": 302, "stage_name": "Agendada", "is_editable": True},
    # Explicit override candidates
    999: {"stage_id": 999, "stage_name": "CustomWon", "is_editable": True},
}


# ── Tests: output contract ────────────────────────────────────────────────────

class TestAgentMetricsContract:
    def test_returns_agent_metrics(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert isinstance(m, AgentMetrics)

    def test_agent_id_preserved(self) -> None:
        m = MetricsCalculator().calculate(make_profile("agent-007"))
        assert m.agent_id == "agent-007"

    def test_all_int_fields_non_negative(self) -> None:
        m = MetricsCalculator().calculate(
            make_profile(leads=[make_lead()], calls=[make_call()])
        )
        assert m.total_leads >= 0
        assert m.converted_leads >= 0
        assert m.lost_leads >= 0
        assert m.active_leads >= 0
        assert m.total_calls >= 0
        assert m.inbound_calls >= 0
        assert m.outbound_calls >= 0

    def test_all_rates_in_0_1(self) -> None:
        m = MetricsCalculator().calculate(
            make_profile(leads=[make_lead()], calls=[make_call()])
        )
        for field in ["conversion_rate", "call_coverage_rate",
                      "activity_consistency", "responsiveness_proxy",
                      "engagement_score"]:
            v = getattr(m, field)
            assert 0.0 <= v <= 1.0, f"{field}={v} out of range"

    def test_floats_rounded_to_4dp(self) -> None:
        m = MetricsCalculator().calculate(
            make_profile(leads=[make_lead()], calls=[make_call(duration=333)])
        )
        for field in ["conversion_rate", "avg_call_duration",
                      "leads_to_calls_ratio", "call_coverage_rate",
                      "activity_consistency", "responsiveness_proxy",
                      "engagement_score"]:
            v = getattr(m, field)
            assert v == round(v, 4), f"{field}={v} has more than 4dp"

    def test_is_frozen(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        with pytest.raises((AttributeError, TypeError)):
            m.total_leads = 999  # type: ignore[misc]

    def test_to_dict_has_all_keys(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        d = m.to_dict()
        expected = {
            "agent_id", "total_leads", "converted_leads", "lost_leads",
            "active_leads", "conversion_rate", "total_calls",
            "avg_call_duration", "inbound_calls", "outbound_calls",
            "leads_to_calls_ratio", "call_coverage_rate",
            "activity_consistency", "responsiveness_proxy", "engagement_score",
        }
        assert expected.issubset(d.keys())


# ── Tests: lead classification ────────────────────────────────────────────────

class TestLeadClassification:
    def test_loss_reason_id_is_lost(self) -> None:
        lead = make_lead(loss_reason_id=12345)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1
        assert m.converted_leads == 0

    def test_closed_without_loss_is_won(self) -> None:
        lead = make_lead(closed_at=1748390400, loss_reason_id=None)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.converted_leads == 1
        assert m.lost_leads == 0

    def test_closed_with_loss_is_lost(self) -> None:
        lead = make_lead(closed_at=1748390400, loss_reason_id=999)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1
        assert m.converted_leads == 0

    def test_no_signals_is_active(self) -> None:
        lead = make_lead(loss_reason_id=None, closed_at=None)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.active_leads == 1
        assert m.converted_leads == 0
        assert m.lost_leads == 0

    def test_stage_map_won_keyword(self) -> None:
        lead = make_lead(status_id=142)
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.converted_leads == 1

    def test_stage_map_lost_keyword(self) -> None:
        lead = make_lead(status_id=143)
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1

    def test_editable_stage_stays_active_despite_name(self) -> None:
        """'Frios' (cold) stage is editable → still classified active."""
        lead = make_lead(status_id=300)  # Frios, editable
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.active_leads == 1
        assert m.lost_leads == 0

    def test_loss_reason_overrides_stage_map_won(self) -> None:
        """loss_reason_id takes priority over stage name."""
        lead = make_lead(status_id=142, loss_reason_id=99)  # stage=won, but has loss reason
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1
        assert m.converted_leads == 0

    def test_non_editable_neutral_stage_is_active(self) -> None:
        """Non-editable 'Incoming leads' has no won/lost keyword → active."""
        lead = make_lead(status_id=200)
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.active_leads == 1

    def test_mixed_leads_correct_counts(self) -> None:
        leads = [
            make_lead(loss_reason_id=None, closed_at=1748390400),  # won
            make_lead(loss_reason_id=None, closed_at=1748390400),  # won
            make_lead(loss_reason_id=555),                          # lost
            make_lead(),                                             # active
        ]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.total_leads == 4
        assert m.converted_leads == 2
        assert m.lost_leads == 1
        assert m.active_leads == 1


# ── Tests: Kommo metrics ──────────────────────────────────────────────────────

class TestKommoMetrics:
    def test_total_leads_count(self) -> None:
        leads = [make_lead() for _ in range(7)]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.total_leads == 7

    def test_conversion_rate_zero_when_no_leads(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert m.conversion_rate == 0.0

    def test_conversion_rate_formula(self) -> None:
        leads = [
            make_lead(closed_at=1, loss_reason_id=None),  # won
            make_lead(closed_at=1, loss_reason_id=None),  # won
            make_lead(loss_reason_id=5),                   # lost
            make_lead(),                                    # active
        ]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.conversion_rate == round(2 / 4, 4)

    def test_conversion_rate_100_percent(self) -> None:
        leads = [make_lead(closed_at=1, loss_reason_id=None) for _ in range(5)]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.conversion_rate == 1.0

    def test_active_leads_never_negative(self) -> None:
        """active_leads = total - converted - lost, clamped to ≥ 0."""
        leads = [
            make_lead(closed_at=1, loss_reason_id=None),
            make_lead(loss_reason_id=5),
        ]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.active_leads >= 0


# ── Tests: Rinkel metrics ─────────────────────────────────────────────────────

class TestRinkelMetrics:
    def test_total_calls_count(self) -> None:
        calls = [make_call() for _ in range(5)]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        assert m.total_calls == 5

    def test_inbound_outbound_counts(self) -> None:
        calls = [
            make_call(direction="inbound"),
            make_call(direction="in"),
            make_call(direction="outbound"),
            make_call(direction="out"),
            make_call(direction="outgoing"),
        ]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        assert m.inbound_calls == 2
        assert m.outbound_calls == 3

    def test_avg_call_duration(self) -> None:
        calls = [
            make_call(duration=100),
            make_call(duration=200),
            make_call(duration=300),
        ]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        assert m.avg_call_duration == round((100 + 200 + 300) / 3, 4)

    def test_avg_duration_zero_when_no_calls(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert m.avg_call_duration == 0.0

    def test_zero_duration_excluded_from_avg(self) -> None:
        calls = [make_call(duration=0), make_call(duration=200)]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        assert m.avg_call_duration == 200.0

    def test_duration_from_duration_seconds_field(self) -> None:
        call = {"call_id": "C1", "agent_id": "1", "duration_seconds": 150}
        m = MetricsCalculator().calculate(make_profile(calls=[call]))
        assert m.avg_call_duration == 150.0

    def test_unknown_direction_not_counted(self) -> None:
        calls = [make_call(direction="internal"), make_call(direction="unknown")]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        assert m.inbound_calls == 0
        assert m.outbound_calls == 0
        assert m.total_calls == 2


# ── Tests: cross-system metrics ───────────────────────────────────────────────

class TestCrossMetrics:
    def test_leads_to_calls_ratio(self) -> None:
        leads = [make_lead() for _ in range(4)]
        calls = [make_call() for _ in range(2)]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        assert m.leads_to_calls_ratio == round(2 / 4, 4)

    def test_ratio_zero_when_no_leads(self) -> None:
        m = MetricsCalculator().calculate(make_profile(calls=[make_call()]))
        assert m.leads_to_calls_ratio == 0.0

    def test_ratio_zero_when_no_calls(self) -> None:
        m = MetricsCalculator().calculate(make_profile(leads=[make_lead()]))
        assert m.leads_to_calls_ratio == 0.0

    def test_call_coverage_capped_at_1(self) -> None:
        """More calls than leads → coverage capped at 1.0."""
        leads = [make_lead()]
        calls = [make_call() for _ in range(10)]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        assert m.call_coverage_rate == 1.0

    def test_activity_consistency_both_systems(self) -> None:
        m = MetricsCalculator().calculate(
            make_profile(leads=[make_lead()], calls=[make_call()])
        )
        assert m.activity_consistency == 1.0

    def test_activity_consistency_kommo_only(self) -> None:
        m = MetricsCalculator().calculate(make_profile(leads=[make_lead()]))
        assert m.activity_consistency == 0.5

    def test_activity_consistency_rinkel_only(self) -> None:
        m = MetricsCalculator().calculate(make_profile(calls=[make_call()]))
        assert m.activity_consistency == 0.5

    def test_activity_consistency_no_data(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert m.activity_consistency == 0.0

    def test_responsiveness_proxy_zero_without_calls(self) -> None:
        leads = [make_lead(closed_at=1, loss_reason_id=None)]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        assert m.responsiveness_proxy == 0.0

    def test_responsiveness_proxy_when_matched(self) -> None:
        leads = [make_lead(closed_at=1, loss_reason_id=None)]  # 100% conversion
        calls = [make_call()]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        # conversion=1.0, coverage=1.0 → proxy = 1.0 * 1.0
        assert m.responsiveness_proxy == 1.0


# ── Tests: engagement score ───────────────────────────────────────────────────

class TestEngagementScore:
    def test_all_zero_when_no_data(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert m.engagement_score == 0.0

    def test_max_score_perfect_agent(self) -> None:
        """An agent with 100% conversion, 1 call per lead, and both systems active."""
        leads = [make_lead(closed_at=1, loss_reason_id=None)]
        calls = [make_call()]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        # 0.4*1.0 + 0.4*1.0 + 0.2*1.0 = 1.0
        assert m.engagement_score == 1.0

    def test_formula_components(self) -> None:
        # 2 leads (0 converted), 1 call (ratio=0.5, coverage=0.5, consistency=1.0)
        leads = [make_lead(), make_lead()]
        calls = [make_call()]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        expected = round(0.40 * 0.0 + 0.40 * 0.5 + 0.20 * 1.0, 4)
        assert m.engagement_score == expected

    def test_kommo_only_agent_score(self) -> None:
        """Kommo-only agent: no calls → ratio=0, consistency=0.5."""
        leads = [make_lead(closed_at=1, loss_reason_id=None) for _ in range(3)]
        m = MetricsCalculator().calculate(make_profile(leads=leads))
        expected = round(0.40 * 1.0 + 0.40 * 0.0 + 0.20 * 0.5, 4)
        assert m.engagement_score == expected

    def test_rinkel_only_agent_score(self) -> None:
        """Rinkel-only agent: no leads → conversion=0, ratio=0, consistency=0.5."""
        calls = [make_call() for _ in range(3)]
        m = MetricsCalculator().calculate(make_profile(calls=calls))
        # 0.4*0 + 0.4*0 + 0.2*0.5 = 0.1
        expected = round(0.20 * 0.5, 4)
        assert m.engagement_score == expected

    def test_score_bounded_between_0_and_1(self) -> None:
        # Run with extreme data
        leads = [make_lead(closed_at=1, loss_reason_id=None) for _ in range(100)]
        calls = [make_call() for _ in range(1000)]
        m = MetricsCalculator().calculate(make_profile(leads=leads, calls=calls))
        assert 0.0 <= m.engagement_score <= 1.0


# ── Tests: safety / never raises ─────────────────────────────────────────────

class TestSafety:
    def test_none_input_returns_null_metrics(self) -> None:
        m = MetricsCalculator().calculate(None)
        assert m.total_leads == 0
        assert m.engagement_score == 0.0

    def test_string_input_returns_null_metrics(self) -> None:
        m = MetricsCalculator().calculate("not a profile")
        assert m.agent_id == "<invalid>"

    def test_int_input_returns_null_metrics(self) -> None:
        m = MetricsCalculator().calculate(42)
        assert m.total_leads == 0

    def test_non_dict_leads_skipped(self) -> None:
        profile = AgentUnifiedProfile("X", ["not a dict", None, 42], [])
        m = MetricsCalculator().calculate(profile)
        assert m.total_leads == 3   # count includes bad records
        assert m.converted_leads == 0

    def test_non_dict_calls_skipped(self) -> None:
        profile = AgentUnifiedProfile("X", [], ["bad", None])
        m = MetricsCalculator().calculate(profile)
        assert m.total_calls == 2
        assert m.inbound_calls == 0

    def test_null_loss_reason_id_bool_rejected(self) -> None:
        """loss_reason_id=False must NOT be treated as a loss."""
        lead = make_lead(loss_reason_id=False, closed_at=None)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 0

    def test_null_closed_at_bool_rejected(self) -> None:
        """closed_at=False must NOT be treated as closed."""
        lead = make_lead(closed_at=False, loss_reason_id=None)
        m = MetricsCalculator().calculate(make_profile(leads=[lead]))
        assert m.converted_leads == 0

    def test_bool_duration_excluded(self) -> None:
        call = {"call_id": "C1", "agent_id": "1", "duration": True}
        m = MetricsCalculator().calculate(make_profile(calls=[call]))
        assert m.avg_call_duration == 0.0

    def test_empty_profile_all_zeros(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        for field in ["total_leads", "converted_leads", "lost_leads",
                      "active_leads", "total_calls", "inbound_calls", "outbound_calls"]:
            assert getattr(m, field) == 0
        for field in ["conversion_rate", "avg_call_duration", "leads_to_calls_ratio",
                      "call_coverage_rate", "activity_consistency",
                      "responsiveness_proxy", "engagement_score"]:
            assert getattr(m, field) == 0.0


# ── Tests: calculate_many() ──────────────────────────────────────────────────

class TestCalculateMany:
    def test_returns_list(self) -> None:
        result = MetricsCalculator().calculate_many([make_profile()])
        assert isinstance(result, list)

    def test_length_matches_input(self) -> None:
        profiles = [make_profile(str(i)) for i in range(5)]
        result = MetricsCalculator().calculate_many(profiles)
        assert len(result) == 5

    def test_order_preserved(self) -> None:
        profiles = [make_profile(str(i)) for i in range(5)]
        result = MetricsCalculator().calculate_many(profiles)
        for i, m in enumerate(result):
            assert m.agent_id == str(i)

    def test_bad_records_do_not_interrupt(self) -> None:
        profiles = [make_profile("1"), None, "bad", make_profile("4")]
        result = MetricsCalculator().calculate_many(profiles)
        assert len(result) == 4
        assert result[0].agent_id == "1"
        assert result[1].agent_id == "<invalid>"
        assert result[3].agent_id == "4"

    def test_non_iterable_returns_empty(self) -> None:
        assert MetricsCalculator().calculate_many(None) == []
        assert MetricsCalculator().calculate_many(42) == []

    def test_empty_list_returns_empty(self) -> None:
        assert MetricsCalculator().calculate_many([]) == []


# ── Tests: determinism ────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        profile = make_profile(
            leads=[make_lead(closed_at=1, loss_reason_id=None), make_lead(loss_reason_id=5)],
            calls=[make_call(duration=200)],
        )
        calc = MetricsCalculator()
        assert calc.calculate(profile) == calc.calculate(profile)

    def test_two_instances_same_result(self) -> None:
        profile = make_profile(leads=[make_lead()], calls=[make_call()])
        assert MetricsCalculator().calculate(profile) == MetricsCalculator().calculate(profile)

    def test_input_not_mutated(self) -> None:
        import copy
        profile = make_profile(leads=[make_lead()], calls=[make_call()])
        original_leads = copy.deepcopy(profile.kommo_leads)
        MetricsCalculator().calculate(profile)
        assert profile.kommo_leads == original_leads


# ── Tests: stage map classification ──────────────────────────────────────────

class TestStageMap:
    def test_non_editable_won_keyword_classified_won(self) -> None:
        stage_map = {50: {"stage_name": "Leads ganados", "is_editable": False}}
        lead = make_lead(status_id=50)
        m = MetricsCalculator(stage_map=stage_map).calculate(make_profile(leads=[lead]))
        assert m.converted_leads == 1

    def test_non_editable_lost_keyword_classified_lost(self) -> None:
        stage_map = {51: {"stage_name": "Leads perdidos", "is_editable": False}}
        lead = make_lead(status_id=51)
        m = MetricsCalculator(stage_map=stage_map).calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1

    def test_editable_stage_always_active(self) -> None:
        stage_map = {52: {"stage_name": "Leads ganados", "is_editable": True}}
        lead = make_lead(status_id=52)
        m = MetricsCalculator(stage_map=stage_map).calculate(make_profile(leads=[lead]))
        assert m.active_leads == 1

    def test_unknown_stage_id_falls_back_to_signals(self) -> None:
        lead = make_lead(status_id=9999999, loss_reason_id=10)
        m = MetricsCalculator(stage_map=STAGE_MAP).calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1


# ── Tests: explicit stage ID overrides ───────────────────────────────────────

class TestExplicitStageIds:
    def test_won_stage_id_override(self) -> None:
        """An editable stage forced into 'won' via won_stage_ids."""
        lead = make_lead(status_id=999)
        m = MetricsCalculator(
            stage_map=STAGE_MAP,
            won_stage_ids={999},
        ).calculate(make_profile(leads=[lead]))
        assert m.converted_leads == 1

    def test_lost_stage_id_override(self) -> None:
        lead = make_lead(status_id=999)
        m = MetricsCalculator(
            stage_map=STAGE_MAP,
            lost_stage_ids={999},
        ).calculate(make_profile(leads=[lead]))
        assert m.lost_leads == 1


# ── Tests: to_dict() ─────────────────────────────────────────────────────────

class TestToDict:
    def test_returns_dict(self) -> None:
        m = MetricsCalculator().calculate(make_profile())
        assert isinstance(m.to_dict(), dict)

    def test_all_values_numeric_or_str(self) -> None:
        m = MetricsCalculator().calculate(make_profile(leads=[make_lead()], calls=[make_call()]))
        for k, v in m.to_dict().items():
            assert isinstance(v, (str, int, float)), f"{k}: {type(v)}"

    def test_repr_contains_agent_id(self) -> None:
        m = MetricsCalculator().calculate(make_profile("my-agent"))
        assert "my-agent" in repr(m)


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        from app.services.kommo_audit_service import KommoAuditService
        self.svc = KommoAuditService(exports_dir=str(REAL_EXPORTS))
        provider = KommoProvider(REAL_EXPORTS)
        self.calculator = MetricsCalculator(stage_map=provider.stages_by_id())
        self.profiles = self.svc.agent_profiles()
        self.all_metrics = self.calculator.calculate_many(self.profiles)

    def test_count_matches_profiles(self) -> None:
        assert len(self.all_metrics) == len(self.profiles)

    def test_all_are_agent_metrics(self) -> None:
        assert all(isinstance(m, AgentMetrics) for m in self.all_metrics)

    def test_no_exceptions_raised(self) -> None:
        assert True  # reaching here means no exceptions in setup_method

    def test_total_leads_sum(self) -> None:
        total = sum(m.total_leads for m in self.all_metrics)
        assert total == 448

    def test_all_rates_in_range(self) -> None:
        for m in self.all_metrics:
            for field in ["conversion_rate", "call_coverage_rate",
                          "activity_consistency", "engagement_score"]:
                v = getattr(m, field)
                assert 0.0 <= v <= 1.0, f"Agent {m.agent_id} {field}={v}"

    def test_known_lost_leads_detected(self) -> None:
        """Real data has 77 leads with loss_reason_id set → all should be lost."""
        total_lost = sum(m.lost_leads for m in self.all_metrics)
        assert total_lost >= 77, f"Expected ≥77 lost leads, got {total_lost}"

    def test_known_won_leads_detected(self) -> None:
        """Real data has 1 lead in 'Leads ganados' stage."""
        total_won = sum(m.converted_leads for m in self.all_metrics)
        # closed_at without loss_reason also counts — at least the ganados lead
        assert total_won >= 1

    def test_engagement_scores_deterministic(self) -> None:
        second_pass = self.calculator.calculate_many(self.profiles)
        for m1, m2 in zip(self.all_metrics, second_pass):
            assert m1.engagement_score == m2.engagement_score

    def test_floats_rounded(self) -> None:
        for m in self.all_metrics:
            assert m.conversion_rate == round(m.conversion_rate, 4)
            assert m.engagement_score == round(m.engagement_score, 4)

    def test_activity_consistency_kommo_only(self) -> None:
        """Without Rinkel calls, all agents should be 0.5 (kommo-only)."""
        for m in self.all_metrics:
            assert m.activity_consistency == 0.5
