"""
Tests for ClaudeAuditExecutor.

All tests run in dry_run=True mode — no real Claude API calls are made.
The test suite covers:

  TestInit               — construction, repr, dry_run fallback
  TestOutputContract     — ClaudeAuditResult fields and types
  TestDryRun             — dry_run returns skipped results
  TestSkipKommoOnly      — skip_kommo_only filter
  TestPromptConstruction — _build_prompt output shape and content
  TestScoreParser        — _parse_scores with valid/fenced/partial/bad JSON
  TestExecuteOne         — single-agent path
  TestExecuteMany        — batch path, concurrency, order
  TestSafetyGauntlet     — None, bad objects, non-list inputs
  TestSummarise          — aggregate summary dict
  TestClaudeScores       — frozen dataclass contract
  TestSimulatedOkResult  — inject mocked Claude response end-to-end
  TestRealData           — dry_run smoke test against 448 real leads
"""

from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.claude_audit_executor import (
    ClaudeAuditExecutor,
    ClaudeAuditResult,
    ClaudeScores,
    _parse_scores,
)
from app.services.audit_engine import (
    AuditEngine,
    AgentAuditReport,
    KommoSection,
    RinkelSection,
    CombinedSection,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_report(
    agent_id: str = "5001",
    total_leads: int = 10,
    converted: int = 3,
    lost: int = 2,
    active: int = 5,
    total_calls: int = 5,
    inbound: int = 3,
    outbound: int = 2,
    avg_duration: float = 120.0,
    engagement: float = 0.4,
    performance: float = 0.35,
    consistency: float = 0.5,
) -> AgentAuditReport:
    kommo = KommoSection(
        total_leads=total_leads,
        converted_leads=converted,
        lost_leads=lost,
        active_leads=active,
        conversion_rate=round(converted / max(total_leads, 1), 4),
    )
    rinkel = RinkelSection(
        total_calls=total_calls,
        avg_call_duration=avg_duration,
        inbound_calls=inbound,
        outbound_calls=outbound,
        engagement_score=engagement,
    )
    combined = CombinedSection(
        performance_score=performance,
        activity_consistency=consistency,
        leads_to_calls_ratio=round(total_calls / max(total_leads, 1), 4),
        responsiveness_proxy=0.5,
        data_source_flags={"kommo": total_leads > 0, "rinkel": total_calls > 0},
    )
    return AgentAuditReport(
        agent_id=agent_id,
        kommo=kommo,
        rinkel=rinkel,
        combined=combined,
        normalized_leads=(),
    )


_VALID_CLAUDE_JSON = json.dumps({
    "overall_score": 3.5,
    "conversion_assessment": "Moderate conversion rate.",
    "call_activity_note": "Good call volume.",
    "strengths": ["Consistent follow-up", "High call volume"],
    "coaching_points": ["Improve conversion", "Reduce lost leads"],
    "risk_level": "low",
    "data_quality": "HIGH",
})

_FENCED_CLAUDE_JSON = f"```json\n{_VALID_CLAUDE_JSON}\n```"

_PARTIAL_JSON = '{"overall_score": 4.0, "risk_level": "none"}'


def _run(coro):
    """Run an async coroutine in tests."""
    return asyncio.run(coro)


# ── Tests: init ───────────────────────────────────────────────────────────────

class TestInit:
    def test_dry_run_construction(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert e is not None

    def test_repr_contains_model(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert "claude" in repr(e).lower()

    def test_repr_contains_dry_run(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert "dry_run=True" in repr(e)

    def test_custom_model_in_repr(self) -> None:
        e = ClaudeAuditExecutor(model="claude-haiku-4-5", dry_run=True)
        assert "haiku" in repr(e)

    def test_concurrency_default(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert e._concurrency == 3


# ── Tests: output contract ────────────────────────────────────────────────────

class TestOutputContract:
    def test_result_is_claude_audit_result(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        result = _run(e.execute_one(_make_report()))
        assert isinstance(result, ClaudeAuditResult)

    def test_agent_id_preserved(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        result = _run(e.execute_one(_make_report("agent-007")))
        assert result.agent_id == "agent-007"

    def test_status_is_valid(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert r.status in {"ok", "skipped", "error"}

    def test_is_frozen(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        with pytest.raises((AttributeError, TypeError)):
            r.agent_id = "hacked"  # type: ignore[misc]

    def test_to_dict_has_required_keys(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        d = r.to_dict()
        assert set(d.keys()) >= {
            "agent_id", "status", "prompt_tokens",
            "completion_tokens", "model", "executed_at",
        }

    def test_repr_contains_agent_id(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report("xyz")))
        assert "xyz" in repr(r)

    def test_executed_at_is_iso(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert "T" in r.executed_at


# ── Tests: dry_run ────────────────────────────────────────────────────────────

class TestDryRun:
    def test_status_is_skipped(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert r.status == "skipped"

    def test_no_scores(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert r.scores is None

    def test_no_error(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert r.error is None

    def test_zero_tokens(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0

    def test_batch_all_skipped(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = _run(e.execute([_make_report("1"), _make_report("2")]))
        assert all(r.status == "skipped" for r in results)


# ── Tests: skip_kommo_only ────────────────────────────────────────────────────

class TestSkipKommoOnly:
    def test_skips_agent_with_no_calls(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True, skip_kommo_only=True)
        report = _make_report(total_calls=0)
        r = _run(e.execute_one(report))
        assert r.status == "skipped"

    def test_does_not_skip_agent_with_calls(self) -> None:
        """With dry_run=True, matched agents still return 'skipped', not 'error'."""
        e = ClaudeAuditExecutor(dry_run=True, skip_kommo_only=True)
        report = _make_report(total_calls=3)
        r = _run(e.execute_one(report))
        # dry_run means always skipped but status should not be "error"
        assert r.status != "error"

    def test_default_does_not_skip_kommo_only(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True, skip_kommo_only=False)
        report = _make_report(total_calls=0)
        r = _run(e.execute_one(report))
        assert r.status == "skipped"  # dry_run, but not filtered before dry_run check


# ── Tests: prompt construction ────────────────────────────────────────────────

class TestPromptConstruction:
    def test_returns_two_strings(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        system, user = e._build_prompt(_make_report())
        assert isinstance(system, str)
        assert isinstance(user, str)

    def test_system_non_empty(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        system, _ = e._build_prompt(_make_report())
        assert len(system) > 50

    def test_user_contains_agent_id(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        _, user = e._build_prompt(_make_report("agent-42"))
        assert "agent-42" in user

    def test_user_contains_total_leads(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        _, user = e._build_prompt(_make_report(total_leads=99))
        assert "99" in user

    def test_user_contains_conversion_rate(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        _, user = e._build_prompt(_make_report(total_leads=4, converted=2))
        assert "0.5000" in user

    def test_user_contains_json_template(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        _, user = e._build_prompt(_make_report())
        assert "overall_score" in user
        assert "risk_level" in user

    def test_user_contains_no_call_data_note_when_zero_calls(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        _, user = e._build_prompt(_make_report(total_calls=0))
        assert "0" in user  # total_calls: 0 present

    def test_deterministic(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _make_report()
        p1 = e._build_prompt(r)
        p2 = e._build_prompt(r)
        assert p1 == p2


# ── Tests: score parser ───────────────────────────────────────────────────────

class TestScoreParser:
    def test_valid_json(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert scores is not None
        assert scores.overall_score == 3.5

    def test_fenced_json(self) -> None:
        scores = _parse_scores(_FENCED_CLAUDE_JSON)
        assert scores is not None
        assert scores.overall_score == 3.5

    def test_partial_json(self) -> None:
        scores = _parse_scores(_PARTIAL_JSON)
        assert scores is not None
        assert scores.overall_score == 4.0

    def test_empty_string_returns_none(self) -> None:
        assert _parse_scores("") is None

    def test_none_like_string_returns_none(self) -> None:
        assert _parse_scores("null") is None

    def test_pure_text_returns_none(self) -> None:
        assert _parse_scores("I cannot audit this agent.") is None

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_scores("{bad json: [}") is None

    def test_strengths_are_tuple(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert isinstance(scores.strengths, tuple)

    def test_coaching_points_are_tuple(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert isinstance(scores.coaching_points, tuple)

    def test_raw_response_preserved(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert scores.raw_response == _VALID_CLAUDE_JSON

    def test_risk_level_parsed(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert scores.risk_level == "low"

    def test_data_quality_parsed(self) -> None:
        scores = _parse_scores(_VALID_CLAUDE_JSON)
        assert scores.data_quality == "HIGH"

    def test_missing_fields_default(self) -> None:
        """Partial response with only overall_score — should still parse."""
        scores = _parse_scores('{"overall_score": 2.0}')
        assert scores is not None
        assert scores.overall_score == 2.0
        assert scores.strengths == ()

    def test_embedded_json_in_prose(self) -> None:
        """Claude occasionally wraps JSON in prose."""
        text = 'Here is my analysis:\n\n{"overall_score": 4.5, "risk_level": "none"}'
        scores = _parse_scores(text)
        assert scores is not None
        assert scores.overall_score == 4.5


# ── Tests: execute_one ────────────────────────────────────────────────────────

class TestExecuteOne:
    def test_returns_result(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(_make_report()))
        assert isinstance(r, ClaudeAuditResult)

    def test_invalid_input_returns_error_or_skipped(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(None))
        assert r.status in {"error", "skipped"}

    def test_string_input_no_crash(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one("not a report"))
        assert isinstance(r, ClaudeAuditResult)

    def test_int_input_no_crash(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(42))
        assert isinstance(r, ClaudeAuditResult)


# ── Tests: execute (batch) ────────────────────────────────────────────────────

class TestExecuteMany:
    def test_returns_list(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        result = _run(e.execute([_make_report("1"), _make_report("2")]))
        assert isinstance(result, list)

    def test_length_matches_input(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        reports = [_make_report(str(i)) for i in range(5)]
        assert len(_run(e.execute(reports))) == 5

    def test_order_preserved(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        reports = [_make_report(str(i)) for i in range(5)]
        results = _run(e.execute(reports))
        assert [r.agent_id for r in results] == [str(i) for i in range(5)]

    def test_empty_list_returns_empty(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert _run(e.execute([])) == []

    def test_none_input_returns_empty(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        assert _run(e.execute(None)) == []

    def test_bad_records_do_not_interrupt(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        mixed = [_make_report("1"), None, "bad", _make_report("4")]
        results = _run(e.execute(mixed))
        assert len(results) == 4
        assert results[0].agent_id == "1"
        assert results[3].agent_id == "4"


# ── Tests: safety gauntlet ────────────────────────────────────────────────────

class TestSafetyGauntlet:
    def test_none_report_no_crash(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(None))
        assert isinstance(r, ClaudeAuditResult)

    def test_empty_dict_no_crash(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one({}))
        assert isinstance(r, ClaudeAuditResult)

    def test_report_with_missing_sections_no_crash(self) -> None:
        class BadReport:
            agent_id = "x"
        e = ClaudeAuditExecutor(dry_run=True)
        r = _run(e.execute_one(BadReport()))
        assert isinstance(r, ClaudeAuditResult)


# ── Tests: summarise ─────────────────────────────────────────────────────────

class TestSummarise:
    def _make_ok(self, agent_id: str, score: float) -> ClaudeAuditResult:
        return ClaudeAuditResult(
            agent_id=agent_id,
            status="ok",
            prompt_tokens=100,
            completion_tokens=50,
            model="claude-test",
            executed_at="2026-06-01T00:00:00+00:00",
            scores=ClaudeScores(
                overall_score=score,
                conversion_assessment="ok",
                call_activity_note="ok",
                strengths=("a",),
                coaching_points=("b",),
                risk_level="none",
                data_quality="HIGH",
                raw_response="{}",
            ),
        )

    def _make_skipped(self, agent_id: str) -> ClaudeAuditResult:
        return ClaudeAuditResult(
            agent_id=agent_id, status="skipped",
            prompt_tokens=0, completion_tokens=0,
            model="claude-test",
            executed_at="2026-06-01T00:00:00+00:00",
        )

    def test_empty_results(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        s = e.summarise([])
        assert s["total"] == 0
        assert s["top_performer"] is None

    def test_required_keys(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 4.0), self._make_skipped("2")]
        s = e.summarise(results)
        assert set(s.keys()) >= {
            "total", "ok", "skipped", "errors",
            "avg_score", "top_performer", "bottom_performer",
            "total_tokens_used",
        }

    def test_counts(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 4.0), self._make_ok("2", 2.0), self._make_skipped("3")]
        s = e.summarise(results)
        assert s["total"] == 3
        assert s["ok"] == 2
        assert s["skipped"] == 1
        assert s["errors"] == 0

    def test_avg_score(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 4.0), self._make_ok("2", 2.0)]
        s = e.summarise(results)
        assert s["avg_score"] == 3.0

    def test_top_performer(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 4.0), self._make_ok("2", 2.0)]
        s = e.summarise(results)
        assert s["top_performer"] == "1"

    def test_bottom_performer(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 4.0), self._make_ok("2", 2.0)]
        s = e.summarise(results)
        assert s["bottom_performer"] == "2"

    def test_total_tokens(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_ok("1", 3.0)]  # 100 prompt + 50 completion = 150
        s = e.summarise(results)
        assert s["total_tokens_used"] == 150

    def test_all_skipped_avg_score_none(self) -> None:
        e = ClaudeAuditExecutor(dry_run=True)
        results = [self._make_skipped("1"), self._make_skipped("2")]
        s = e.summarise(results)
        assert s["avg_score"] is None


# ── Tests: ClaudeScores dataclass ─────────────────────────────────────────────

class TestClaudeScores:
    def _make_scores(self) -> ClaudeScores:
        return ClaudeScores(
            overall_score=4.0,
            conversion_assessment="Good",
            call_activity_note="Solid",
            strengths=("Follows up", "Polite"),
            coaching_points=("Convert more",),
            risk_level="low",
            data_quality="HIGH",
            raw_response=_VALID_CLAUDE_JSON,
        )

    def test_is_frozen(self) -> None:
        s = self._make_scores()
        with pytest.raises((AttributeError, TypeError)):
            s.overall_score = 5.0  # type: ignore[misc]

    def test_to_dict_has_keys(self) -> None:
        d = self._make_scores().to_dict()
        assert set(d.keys()) >= {
            "overall_score", "conversion_assessment", "call_activity_note",
            "strengths", "coaching_points", "risk_level", "data_quality",
        }

    def test_to_dict_excludes_raw_response(self) -> None:
        """raw_response is for internal use — not exposed in to_dict()."""
        d = self._make_scores().to_dict()
        assert "raw_response" not in d

    def test_strengths_list_in_dict(self) -> None:
        d = self._make_scores().to_dict()
        assert isinstance(d["strengths"], list)


# ── Tests: simulated ok result (mocked Claude) ────────────────────────────────

class TestSimulatedOkResult:
    """
    Mock the Anthropic client to simulate a successful Claude response
    without hitting the actual API.
    """

    def _make_mock_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]
        mock_response.usage.input_tokens  = 250
        mock_response.usage.output_tokens = 80

        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(return_value=mock_response)

        mock_client = MagicMock()
        mock_client.messages = mock_messages
        return mock_client

    def test_ok_status_on_valid_response(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client(_VALID_CLAUDE_JSON)

        r = _run(e.execute_one(_make_report()))
        assert r.status == "ok"

    def test_scores_populated(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client(_VALID_CLAUDE_JSON)

        r = _run(e.execute_one(_make_report()))
        assert r.scores is not None
        assert r.scores.overall_score == 3.5

    def test_token_counts(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client(_VALID_CLAUDE_JSON)

        r = _run(e.execute_one(_make_report()))
        assert r.prompt_tokens     == 250
        assert r.completion_tokens == 80

    def test_fenced_response_parsed(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client(_FENCED_CLAUDE_JSON)

        r = _run(e.execute_one(_make_report()))
        assert r.status == "ok"
        assert r.scores.overall_score == 3.5

    def test_bad_response_returns_error(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client("I cannot parse this.")

        r = _run(e.execute_one(_make_report()))
        assert r.status == "error"
        assert r.scores is None

    def test_api_exception_returns_error(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(side_effect=RuntimeError("API down"))
        mock_client = MagicMock()
        mock_client.messages = mock_messages
        e._client = mock_client

        r = _run(e.execute_one(_make_report()))
        assert r.status == "error"
        assert "API down" in r.error

    def test_batch_with_mock_all_ok(self) -> None:
        e = ClaudeAuditExecutor(dry_run=False, api_key="fake-key")
        e._dry_run = False
        e._client  = self._make_mock_client(_VALID_CLAUDE_JSON)

        reports = [_make_report(str(i)) for i in range(3)]
        results = _run(e.execute(reports))
        assert all(r.status == "ok" for r in results)
        assert all(r.scores is not None for r in results)


# ── Tests: real data smoke test ───────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        engine        = AuditEngine(exports_dir=str(REAL_EXPORTS))
        self.reports  = engine.run()
        self.executor = ClaudeAuditExecutor(dry_run=True)

    def test_execute_returns_one_per_report(self) -> None:
        results = _run(self.executor.execute(self.reports))
        assert len(results) == len(self.reports)

    def test_all_skipped_in_dry_run(self) -> None:
        results = _run(self.executor.execute(self.reports))
        assert all(r.status == "skipped" for r in results)

    def test_all_agent_ids_are_strings(self) -> None:
        results = _run(self.executor.execute(self.reports))
        assert all(isinstance(r.agent_id, str) for r in results)

    def test_prompts_are_deterministic(self) -> None:
        """Same report → same prompt on two calls."""
        report = self.reports[0]
        p1 = self.executor._build_prompt(report)
        p2 = self.executor._build_prompt(report)
        assert p1 == p2

    def test_summarise_all_skipped(self) -> None:
        results = _run(self.executor.execute(self.reports))
        s = self.executor.summarise(results)
        assert s["total"] == len(self.reports)
        assert s["skipped"] == len(self.reports)
        assert s["ok"] == 0
