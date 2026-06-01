"""
Tests for AgentLinkingEngine.

Rules under test:
  - Agents in only one system are still included (never dropped)
  - Original dicts are never mutated
  - Deterministic (sorted output, same input → same output)
  - Explicit agent_id_map cross-references both systems
  - Numeric auto-matching when rinkel agent_id == kommo responsible_user_id
  - Calls with no agent_id placed in __unidentified__ profile
  - Leads with no responsible_user_id are silently skipped
  - link_as_dict() gives O(1) access

Test groups:
  TestOutputContract        — AgentUnifiedProfile field guarantees
  TestEmptyInputs           — both/one/other empty
  TestKommoOnly             — agents with leads but no calls
  TestRinkelOnly            — agents with calls but no leads
  TestMatching              — agents in both systems
  TestExplicitMap           — agent_id_map cross-references
  TestAutoMatch             — numeric rinkel_id == kommo_user_id
  TestNoDataDropped         — every input record appears in output
  TestNonMutation           — original dicts unchanged after link()
  TestDeterminism           — same input → same sorted output
  TestEdgeCases             — nulls, booleans, nested calls, bad inputs
  TestLinkAsDict            — O(1) helper
  TestProperties            — is_matched / is_kommo_only / is_rinkel_only
  TestRealData              — smoke test against 448 real Kommo leads
"""

from __future__ import annotations

import copy
import pytest
from pathlib import Path

from app.services.agent_linking_engine import AgentLinkingEngine, AgentUnifiedProfile
from app.integrations.kommo import KommoProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_lead(user_id: int, **extra) -> dict:
    return {"id": 1000 + user_id, "responsible_user_id": user_id, **extra}


def make_call(agent_id: str, call_id: str = "CALL-001", **extra) -> dict:
    return {"call_id": call_id, "agent_id": agent_id, **extra}


# ── Tests: output contract ────────────────────────────────────────────────────

class TestOutputContract:
    def test_returns_list(self) -> None:
        result = AgentLinkingEngine().link([], [])
        assert isinstance(result, list)

    def test_each_element_is_agent_profile(self) -> None:
        result = AgentLinkingEngine().link(
            [make_lead(1)], [make_call("1")]
        )
        assert all(isinstance(p, AgentUnifiedProfile) for p in result)

    def test_agent_id_is_str(self) -> None:
        result = AgentLinkingEngine().link([make_lead(42)], [])
        assert isinstance(result[0].agent_id, str)

    def test_kommo_leads_is_list(self) -> None:
        result = AgentLinkingEngine().link([make_lead(1)], [])
        assert isinstance(result[0].kommo_leads, list)

    def test_rinkel_calls_is_list(self) -> None:
        result = AgentLinkingEngine().link([], [make_call("X")])
        assert isinstance(result[0].rinkel_calls, list)

    def test_to_dict_keys(self) -> None:
        p = AgentLinkingEngine().link([make_lead(1)], [])[0]
        keys = set(p.to_dict().keys())
        assert keys == {"agent_id", "kommo_leads", "rinkel_calls",
                        "is_matched", "total_leads", "total_calls"}


# ── Tests: empty inputs ───────────────────────────────────────────────────────

class TestEmptyInputs:
    def test_both_empty_returns_empty_list(self) -> None:
        assert AgentLinkingEngine().link([], []) == []

    def test_empty_kommo_with_rinkel_calls(self) -> None:
        result = AgentLinkingEngine().link([], [make_call("agent-001")])
        assert len(result) == 1
        assert result[0].agent_id == "agent-001"
        assert result[0].kommo_leads == []
        assert len(result[0].rinkel_calls) == 1

    def test_empty_rinkel_with_kommo_leads(self) -> None:
        result = AgentLinkingEngine().link([make_lead(5)], [])
        assert len(result) == 1
        assert result[0].agent_id == "5"
        assert result[0].rinkel_calls == []
        assert len(result[0].kommo_leads) == 1


# ── Tests: Kommo-only agents ──────────────────────────────────────────────────

class TestKommoOnly:
    def test_kommo_only_agent_included(self) -> None:
        result = AgentLinkingEngine().link([make_lead(10)], [])
        assert len(result) == 1
        assert result[0].is_kommo_only is True
        assert result[0].is_rinkel_only is False
        assert result[0].is_matched is False

    def test_multiple_kommo_only_agents(self) -> None:
        leads = [make_lead(10), make_lead(20), make_lead(10, id=9999)]
        result = AgentLinkingEngine().link(leads, [])
        ids = {p.agent_id for p in result}
        assert ids == {"10", "20"}
        # agent 10 has 2 leads
        p10 = next(p for p in result if p.agent_id == "10")
        assert p10.total_leads == 2


# ── Tests: Rinkel-only agents ─────────────────────────────────────────────────

class TestRinkelOnly:
    def test_rinkel_only_agent_included(self) -> None:
        result = AgentLinkingEngine().link([], [make_call("sophie")])
        assert len(result) >= 1
        sophie = next(p for p in result if p.agent_id == "sophie")
        assert sophie.is_rinkel_only is True
        assert sophie.is_matched is False

    def test_multiple_calls_same_agent(self) -> None:
        calls = [make_call("sophie", "C1"), make_call("sophie", "C2")]
        result = AgentLinkingEngine().link([], calls)
        sophie = next(p for p in result if p.agent_id == "sophie")
        assert sophie.total_calls == 2


# ── Tests: matched agents (in both systems) ───────────────────────────────────

class TestMatching:
    def test_numeric_auto_match(self) -> None:
        """rinkel agent_id '10359915' matches kommo responsible_user_id 10359915."""
        leads = [make_lead(10359915)]
        calls = [make_call("10359915")]
        result = AgentLinkingEngine().link(leads, calls)
        assert len(result) == 1
        p = result[0]
        assert p.agent_id == "10359915"
        assert p.is_matched is True
        assert p.total_leads == 1
        assert p.total_calls == 1

    def test_matched_agent_has_correct_counts(self) -> None:
        leads = [make_lead(5, id=1), make_lead(5, id=2)]
        calls = [make_call("5", "C1"), make_call("5", "C2"), make_call("5", "C3")]
        result = AgentLinkingEngine().link(leads, calls)
        p = result[0]
        assert p.total_leads == 2
        assert p.total_calls == 3

    def test_mixed_matched_and_unmatched(self) -> None:
        leads = [make_lead(100), make_lead(200)]
        calls = [make_call("100"), make_call("unknown-agent")]
        result = AgentLinkingEngine().link(leads, calls)

        ids = {p.agent_id for p in result}
        assert "100" in ids          # matched
        assert "200" in ids          # kommo-only
        assert "unknown-agent" in ids  # rinkel-only

        matched = next(p for p in result if p.agent_id == "100")
        assert matched.is_matched is True

        kommo_only = next(p for p in result if p.agent_id == "200")
        assert kommo_only.is_kommo_only is True

        rinkel_only = next(p for p in result if p.agent_id == "unknown-agent")
        assert rinkel_only.is_rinkel_only is True


# ── Tests: explicit agent_id_map ──────────────────────────────────────────────

class TestExplicitMap:
    def test_explicit_map_links_different_id_formats(self) -> None:
        """Rinkel 'agent-nl-007' → Kommo user 10359915."""
        leads = [make_lead(10359915)]
        calls = [make_call("agent-nl-007")]
        engine = AgentLinkingEngine(agent_id_map={"agent-nl-007": "10359915"})
        result = engine.link(leads, calls)

        assert len(result) == 1
        p = result[0]
        assert p.agent_id == "10359915"
        assert p.is_matched is True
        assert p.total_leads == 1
        assert p.total_calls == 1

    def test_explicit_map_overrides_auto_match(self) -> None:
        """When explicit map provides a mapping, auto-match does not create a duplicate."""
        leads = [make_lead(10359915)]
        calls = [make_call("10359915")]
        # Explicit map for same pair
        engine = AgentLinkingEngine(agent_id_map={"10359915": "10359915"})
        result = engine.link(leads, calls)
        # Should produce 1 profile, not 2
        assert len(result) == 1
        assert result[0].is_matched is True

    def test_unmapped_rinkel_agent_still_included(self) -> None:
        """Rinkel agents not in agent_id_map are still included as rinkel-only."""
        leads = [make_lead(10359915)]
        calls = [make_call("agent-nl-007"), make_call("orphan-agent")]
        engine = AgentLinkingEngine(agent_id_map={"agent-nl-007": "10359915"})
        result = engine.link(leads, calls)

        ids = {p.agent_id for p in result}
        assert "10359915" in ids       # matched
        assert "orphan-agent" in ids   # rinkel-only

    def test_multiple_rinkel_agents_map_to_same_kommo_user(self) -> None:
        """Last explicit mapping wins for duplicate kommo targets."""
        leads = [make_lead(999)]
        calls = [make_call("rinkel-A"), make_call("rinkel-B")]
        # Both rinkel IDs map to same kommo user — rinkel-B takes precedence (last)
        engine = AgentLinkingEngine(
            agent_id_map={"rinkel-A": "999", "rinkel-B": "999"}
        )
        result = engine.link(leads, calls)
        # One profile for kommo user 999, one orphan for the un-used rinkel ID
        profile_999 = next((p for p in result if p.agent_id == "999"), None)
        assert profile_999 is not None
        # The linked one is matched; the other is rinkel-only
        matched = [p for p in result if p.is_matched]
        assert len(matched) == 1

    def test_map_with_none_values_ignored(self) -> None:
        """None keys/values in agent_id_map should not crash."""
        engine = AgentLinkingEngine(agent_id_map={None: "123", "abc": None})
        result = engine.link([make_lead(123)], [])
        assert len(result) == 1

    def test_map_with_whitespace_stripped(self) -> None:
        leads = [make_lead(10359915)]
        calls = [make_call("  agent-nl-007  ")]
        engine = AgentLinkingEngine(agent_id_map={"agent-nl-007": "10359915"})
        result = engine.link(leads, calls)
        # Note: call agent_id "  agent-nl-007  " strips to "agent-nl-007"
        # which matches the map key
        assert any(p.is_matched for p in result)


# ── Tests: no data dropped ────────────────────────────────────────────────────

class TestNoDataDropped:
    def test_total_leads_preserved(self) -> None:
        leads = [make_lead(1), make_lead(2), make_lead(3)]
        result = AgentLinkingEngine().link(leads, [])
        total = sum(p.total_leads for p in result)
        assert total == 3

    def test_total_calls_preserved(self) -> None:
        calls = [make_call("A", "C1"), make_call("B", "C2"), make_call("A", "C3")]
        result = AgentLinkingEngine().link([], calls)
        total = sum(p.total_calls for p in result)
        assert total == 3

    def test_calls_without_agent_id_not_dropped(self) -> None:
        """Calls with no agent_id must appear in the __unidentified__ profile."""
        calls = [
            {"call_id": "C1", "agent_id": None},
            {"call_id": "C2"},
        ]
        result = AgentLinkingEngine().link([], calls)
        total_calls = sum(p.total_calls for p in result)
        assert total_calls == 2

    def test_leads_without_user_id_are_skipped_not_crashed(self) -> None:
        """Leads with no responsible_user_id are skipped (no crash)."""
        leads = [
            make_lead(1),
            {"id": 9999},             # no responsible_user_id
            {"responsible_user_id": None},
        ]
        result = AgentLinkingEngine().link(leads, [])
        # Only valid lead (user 1) appears
        assert any(p.agent_id == "1" for p in result)

    def test_no_profile_is_empty_when_data_exists(self) -> None:
        """Every profile must have at least one lead or one call."""
        leads = [make_lead(1), make_lead(2)]
        calls = [make_call("3"), make_call("1")]
        result = AgentLinkingEngine().link(leads, calls)
        for p in result:
            assert p.total_leads + p.total_calls > 0


# ── Tests: non-mutation ───────────────────────────────────────────────────────

class TestNonMutation:
    def test_kommo_leads_not_mutated(self) -> None:
        leads = [make_lead(1, extra_field="original")]
        original = copy.deepcopy(leads)
        AgentLinkingEngine().link(leads, [])
        assert leads == original

    def test_rinkel_calls_not_mutated(self) -> None:
        calls = [make_call("X", extra_field="original")]
        original = copy.deepcopy(calls)
        AgentLinkingEngine().link([], calls)
        assert calls == original

    def test_profile_leads_are_same_objects(self) -> None:
        """Dicts in profile.kommo_leads must be the SAME objects (not copies)."""
        lead = make_lead(1)
        result = AgentLinkingEngine().link([lead], [])
        assert result[0].kommo_leads[0] is lead

    def test_profile_calls_are_same_objects(self) -> None:
        call = make_call("A")
        result = AgentLinkingEngine().link([], [call])
        assert result[0].rinkel_calls[0] is call


# ── Tests: determinism ────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        leads = [make_lead(1), make_lead(2)]
        calls = [make_call("1"), make_call("3")]
        engine = AgentLinkingEngine()
        r1 = engine.link(leads, calls)
        r2 = engine.link(leads, calls)
        assert [p.agent_id for p in r1] == [p.agent_id for p in r2]
        assert [p.total_leads for p in r1] == [p.total_leads for p in r2]
        assert [p.total_calls for p in r1] == [p.total_calls for p in r2]

    def test_output_sorted_by_agent_id(self) -> None:
        leads = [make_lead(30), make_lead(10), make_lead(20)]
        result = AgentLinkingEngine().link(leads, [])
        ids = [p.agent_id for p in result]
        assert ids == sorted(ids)

    def test_input_order_does_not_affect_profile_contents(self) -> None:
        leads1 = [make_lead(1, id=1), make_lead(2, id=2)]
        leads2 = [make_lead(2, id=2), make_lead(1, id=1)]
        r1 = AgentLinkingEngine().link(leads1, [])
        r2 = AgentLinkingEngine().link(leads2, [])
        # Same profiles regardless of input order
        assert {p.agent_id for p in r1} == {p.agent_id for p in r2}


# ── Tests: edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_non_list_kommo_input_handled(self) -> None:
        result = AgentLinkingEngine().link(None, [make_call("A")])
        # Should still produce a rinkel-only profile for "A"
        assert any(p.agent_id == "A" for p in result)

    def test_non_list_rinkel_input_handled(self) -> None:
        result = AgentLinkingEngine().link([make_lead(1)], "not a list")
        assert any(p.agent_id == "1" for p in result)

    def test_bool_responsible_user_id_skipped(self) -> None:
        """False and True must not produce agent_ids '0' or '1'."""
        lead_false = {"id": 1, "responsible_user_id": False}
        lead_true  = {"id": 2, "responsible_user_id": True}
        result = AgentLinkingEngine().link([lead_false, lead_true], [])
        # Neither should produce a profile (booleans rejected)
        assert result == []

    def test_float_user_id_normalised(self) -> None:
        lead = {"id": 1, "responsible_user_id": 10359915.0}
        result = AgentLinkingEngine().link([lead], [])
        assert result[0].agent_id == "10359915"

    def test_string_user_id_accepted(self) -> None:
        lead = {"id": 1, "responsible_user_id": "10359915"}
        result = AgentLinkingEngine().link([lead], [])
        assert result[0].agent_id == "10359915"

    def test_nested_call_data_extracted(self) -> None:
        """agent_id nested under 'data' key is resolved."""
        call = {"call_id": "C1", "data": {"agent_id": "nested-agent"}}
        result = AgentLinkingEngine().link([], [call])
        assert any(p.agent_id == "nested-agent" for p in result)

    def test_non_dict_lead_skipped(self) -> None:
        result = AgentLinkingEngine().link(["not a dict", None, 42], [])
        assert result == []

    def test_non_dict_call_skipped(self) -> None:
        result = AgentLinkingEngine().link([], ["not a dict", None])
        # Non-dict calls produce __unidentified__ profile but no crash
        assert isinstance(result, list)

    def test_very_large_batch(self) -> None:
        """Engine handles 10k leads / 10k calls without error."""
        leads = [make_lead(i % 100, id=i) for i in range(10000)]
        calls = [make_call(str(i % 80), f"C{i}") for i in range(10000)]
        result = AgentLinkingEngine().link(leads, calls)
        assert len(result) >= 100
        total_leads = sum(p.total_leads for p in result)
        total_calls = sum(p.total_calls for p in result)
        assert total_leads == 10000
        assert total_calls == 10000


# ── Tests: link_as_dict() ─────────────────────────────────────────────────────

class TestLinkAsDict:
    def test_returns_dict(self) -> None:
        result = AgentLinkingEngine().link_as_dict([make_lead(1)], [])
        assert isinstance(result, dict)

    def test_keyed_by_agent_id(self) -> None:
        result = AgentLinkingEngine().link_as_dict([make_lead(5)], [])
        assert "5" in result
        assert isinstance(result["5"], AgentUnifiedProfile)

    def test_o1_lookup(self) -> None:
        leads = [make_lead(100), make_lead(200)]
        result = AgentLinkingEngine().link_as_dict(leads, [])
        assert result["100"].total_leads == 1
        assert result["200"].total_leads == 1

    def test_consistent_with_link(self) -> None:
        leads = [make_lead(1), make_lead(2)]
        calls = [make_call("1")]
        as_list = AgentLinkingEngine().link(leads, calls)
        as_dict = AgentLinkingEngine().link_as_dict(leads, calls)
        assert set(as_dict.keys()) == {p.agent_id for p in as_list}


# ── Tests: AgentUnifiedProfile properties ─────────────────────────────────────

class TestProperties:
    def test_is_matched_both_populated(self) -> None:
        p = AgentUnifiedProfile("X", [{"id": 1}], [{"call_id": "C1"}])
        assert p.is_matched is True
        assert p.is_kommo_only is False
        assert p.is_rinkel_only is False

    def test_is_kommo_only(self) -> None:
        p = AgentUnifiedProfile("X", [{"id": 1}], [])
        assert p.is_kommo_only is True
        assert p.is_matched is False
        assert p.is_rinkel_only is False

    def test_is_rinkel_only(self) -> None:
        p = AgentUnifiedProfile("X", [], [{"call_id": "C1"}])
        assert p.is_rinkel_only is True
        assert p.is_matched is False
        assert p.is_kommo_only is False

    def test_totals(self) -> None:
        p = AgentUnifiedProfile("X", [{}] * 3, [{}] * 5)
        assert p.total_leads == 3
        assert p.total_calls == 5

    def test_repr_contains_agent_id(self) -> None:
        p = AgentUnifiedProfile("agent-007", [{}], [{}])
        assert "agent-007" in repr(p)


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        self.kommo_leads = provider.get_leads()
        self.engine = AgentLinkingEngine()

    def test_all_448_leads_preserved(self) -> None:
        result = self.engine.link(self.kommo_leads, [])
        total = sum(p.total_leads for p in result)
        assert total == 448

    def test_no_data_dropped(self) -> None:
        """Total lead count across all profiles must equal input count."""
        result = self.engine.link(self.kommo_leads, [])
        assert sum(p.total_leads for p in result) == len(self.kommo_leads)

    def test_profiles_sorted(self) -> None:
        result = self.engine.link(self.kommo_leads, [])
        ids = [p.agent_id for p in result]
        assert ids == sorted(ids)

    def test_all_agent_ids_are_strings(self) -> None:
        result = self.engine.link(self.kommo_leads, [])
        assert all(isinstance(p.agent_id, str) for p in result)

    def test_kommo_only_agents_exist(self) -> None:
        """With no Rinkel calls, all profiles should be Kommo-only."""
        result = self.engine.link(self.kommo_leads, [])
        assert all(p.is_kommo_only for p in result)

    def test_with_simulated_rinkel_calls(self) -> None:
        """Simulate calls using actual Kommo user IDs as agent_ids."""
        from app.integrations.kommo import KommoProvider
        provider = KommoProvider(REAL_EXPORTS)
        mapping = provider.leads_by_responsible_user()
        # Use ALL distinct agents found in real data (may be fewer than 5)
        agent_ids = list(mapping.keys())
        simulated_calls = [
            {"call_id": f"SIM-{aid}", "agent_id": str(aid)}
            for aid in agent_ids
        ]
        result = self.engine.link(self.kommo_leads, simulated_calls)
        matched = [p for p in result if p.is_matched]
        assert len(matched) == len(agent_ids), (
            f"Expected {len(agent_ids)} matched profiles, got {len(matched)}. "
            f"Real agent IDs: {agent_ids}"
        )

    def test_determinism_on_real_data(self) -> None:
        r1 = self.engine.link(self.kommo_leads, [])
        r2 = self.engine.link(self.kommo_leads, [])
        assert [p.agent_id for p in r1] == [p.agent_id for p in r2]
        assert [p.total_leads for p in r1] == [p.total_leads for p in r2]
