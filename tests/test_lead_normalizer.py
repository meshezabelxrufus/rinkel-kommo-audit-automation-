"""
Tests for LeadNormalizer.

Rules under test:
  - Never assume fields exist
  - Never raise on any input
  - Missing values become None
  - All output fields are str | None
  - Deterministic (same input → same output)
  - ISO timestamp preferred over unix fallback
  - Status resolved to stage name when stage_map provided
  - Boolean id/responsible_user_id NOT coerced to "0"/"1"

Test groups:
  TestNormalizedLeadContract     — output type guarantees
  TestFieldExtraction            — each field individually
  TestTimestampResolution        — ISO preferred, unix fallback, None fallback
  TestStatusResolution           — with/without stage_map, fallback to id
  TestSafety                     — non-dict, None, empty, missing fields
  TestNormalizeMany              — batch method
  TestDeterminism                — same input → identical output
  TestToDict                     — to_dict() helper
  TestRealData                   — smoke test against actual 448 leads
"""

from __future__ import annotations

import pytest
from pathlib import Path

from app.services.lead_normalizer import LeadNormalizer, NormalizedLead
from app.integrations.kommo import KommoProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_lead(**overrides) -> dict:
    """Return a realistic lead dict with sensible defaults."""
    base = {
        "id": 25892146,
        "name": "Antonio Hidalgo - Pechos",
        "pipeline_id": 11231784,
        "status_id": 143,
        "responsible_user_id": 10359915,
        "created_at": 1779623803,
        "updated_at": 1779812622,
        "created_at_iso": "2026-05-24T11:56:43+00:00",
        "updated_at_iso": "2026-05-26T16:23:42+00:00",
        "closed_at": None,
        "is_deleted": False,
        "score": None,
        "tags": None,
        "custom_fields_values": [],
    }
    base.update(overrides)
    return base


SAMPLE_STAGE_MAP = {
    143:      {"stage_id": 143,      "stage_name": "Leads perdidos", "pipeline_id": 7677959},
    62386811: {"stage_id": 62386811, "stage_name": "Incoming leads", "pipeline_id": 11231784},
    96235128: {"stage_id": 96235128, "stage_name": "Repesca II",     "pipeline_id": 11231785},
}


# ── Tests: NormalizedLead contract ────────────────────────────────────────────

class TestNormalizedLeadContract:
    def test_all_fields_are_str_or_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        for field, value in lead.to_dict().items():
            assert value is None or isinstance(value, str), (
                f"Field '{field}' is {type(value).__name__}, expected str | None"
            )

    def test_is_frozen_dataclass(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        with pytest.raises((AttributeError, TypeError)):
            lead.id = "mutated"  # type: ignore[misc]

    def test_expected_field_names(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        keys = set(lead.to_dict().keys())
        expected = {"id", "name", "pipeline_id", "responsible_user_id",
                    "created_at", "updated_at", "status"}
        assert keys == expected

    def test_no_extra_fields(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        assert len(lead.to_dict()) == 7


# ── Tests: field extraction ───────────────────────────────────────────────────

class TestFieldExtraction:
    def test_id_as_string(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(id=25892146))
        assert lead.id == "25892146"

    def test_name_extracted(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(name="Test Lead"))
        assert lead.name == "Test Lead"

    def test_pipeline_id_as_string(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(pipeline_id=11231784))
        assert lead.pipeline_id == "11231784"

    def test_responsible_user_id_as_string(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(responsible_user_id=10359915))
        assert lead.responsible_user_id == "10359915"

    def test_created_at_prefers_iso(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso="2026-05-24T11:56:43+00:00",
            created_at=1779623803,
        ))
        assert lead.created_at == "2026-05-24T11:56:43+00:00"

    def test_updated_at_prefers_iso(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            updated_at_iso="2026-05-26T16:23:42+00:00",
            updated_at=1779812622,
        ))
        assert lead.updated_at == "2026-05-26T16:23:42+00:00"

    def test_status_without_stage_map_is_str_of_id(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(status_id=143))
        assert lead.status == "143"

    def test_status_with_stage_map_is_stage_name(self) -> None:
        normalizer = LeadNormalizer(stage_map=SAMPLE_STAGE_MAP)
        lead = normalizer.normalize(make_lead(status_id=143))
        assert lead.status == "Leads perdidos"


# ── Tests: timestamp resolution ───────────────────────────────────────────────

class TestTimestampResolution:
    def test_iso_preferred_over_unix(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso="2026-05-24T11:56:43+00:00",
            created_at=9999999999,
        ))
        assert lead.created_at == "2026-05-24T11:56:43+00:00"

    def test_falls_back_to_unix_when_no_iso(self) -> None:
        raw = make_lead()
        del raw["created_at_iso"]
        lead = LeadNormalizer().normalize(raw)
        assert lead.created_at == "1779623803"

    def test_unix_missing_key_falls_back_to_none(self) -> None:
        raw = make_lead()
        del raw["created_at_iso"]
        del raw["created_at"]
        lead = LeadNormalizer().normalize(raw)
        assert lead.created_at is None

    def test_null_iso_falls_back_to_unix(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso=None,
            created_at=1779623803,
        ))
        assert lead.created_at == "1779623803"

    def test_empty_string_iso_falls_back_to_unix(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso="",
            created_at=1779623803,
        ))
        assert lead.created_at == "1779623803"

    def test_whitespace_iso_falls_back_to_unix(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso="   ",
            created_at=1779623803,
        ))
        assert lead.created_at == "1779623803"

    def test_both_null_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso=None,
            created_at=None,
        ))
        assert lead.created_at is None

    def test_unix_zero_is_valid(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso=None,
            created_at=0,
        ))
        assert lead.created_at == "0"

    def test_float_unix_is_coerced_to_int_str(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso=None,
            created_at=1779623803.7,
        ))
        assert lead.created_at == "1779623803"

    def test_negative_unix_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(
            created_at_iso=None,
            created_at=-1,
        ))
        assert lead.created_at is None


# ── Tests: status resolution ──────────────────────────────────────────────────

class TestStatusResolution:
    def test_no_stage_map_returns_str_of_status_id(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(status_id=96235128))
        assert lead.status == "96235128"

    def test_stage_map_hit_returns_stage_name(self) -> None:
        normalizer = LeadNormalizer(stage_map=SAMPLE_STAGE_MAP)
        lead = normalizer.normalize(make_lead(status_id=96235128))
        assert lead.status == "Repesca II"

    def test_stage_map_miss_falls_back_to_str(self) -> None:
        normalizer = LeadNormalizer(stage_map=SAMPLE_STAGE_MAP)
        lead = normalizer.normalize(make_lead(status_id=9999999))
        assert lead.status == "9999999"

    def test_null_status_id_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(status_id=None))
        assert lead.status is None

    def test_missing_status_id_returns_none(self) -> None:
        raw = make_lead()
        del raw["status_id"]
        lead = LeadNormalizer().normalize(raw)
        assert lead.status is None

    def test_float_status_id_coerced(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(status_id=143.0))
        assert lead.status == "143"

    def test_stage_map_with_whitespace_name_still_works(self) -> None:
        stage_map = {143: {"stage_id": 143, "stage_name": "  Trimmed  "}}
        lead = LeadNormalizer(stage_map=stage_map).normalize(make_lead(status_id=143))
        assert lead.status == "Trimmed"

    def test_stage_map_with_empty_name_falls_back_to_str(self) -> None:
        stage_map = {143: {"stage_id": 143, "stage_name": ""}}
        lead = LeadNormalizer(stage_map=stage_map).normalize(make_lead(status_id=143))
        assert lead.status == "143"

    def test_bool_status_id_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(status_id=True))
        assert lead.status is None


# ── Tests: safety — never raises ─────────────────────────────────────────────

class TestSafety:
    def test_none_input_returns_null_lead(self) -> None:
        lead = LeadNormalizer().normalize(None)
        assert all(v is None for v in lead.to_dict().values())

    def test_string_input_returns_null_lead(self) -> None:
        lead = LeadNormalizer().normalize("not a dict")
        assert all(v is None for v in lead.to_dict().values())

    def test_int_input_returns_null_lead(self) -> None:
        lead = LeadNormalizer().normalize(42)
        assert all(v is None for v in lead.to_dict().values())

    def test_empty_dict_returns_all_none(self) -> None:
        lead = LeadNormalizer().normalize({})
        assert all(v is None for v in lead.to_dict().values())

    def test_completely_null_fields_return_none(self) -> None:
        lead = LeadNormalizer().normalize({
            "id": None,
            "name": None,
            "pipeline_id": None,
            "responsible_user_id": None,
            "created_at": None,
            "updated_at": None,
            "status_id": None,
        })
        assert all(v is None for v in lead.to_dict().values())

    def test_bool_id_returns_none(self) -> None:
        """False and True must not become '0' or '1'."""
        lead = LeadNormalizer().normalize(make_lead(id=False))
        assert lead.id is None

    def test_bool_responsible_user_id_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(responsible_user_id=True))
        assert lead.responsible_user_id is None

    def test_empty_name_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(name=""))
        assert lead.name is None

    def test_whitespace_name_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(name="   "))
        assert lead.name is None

    def test_list_input_returns_null_lead(self) -> None:
        lead = LeadNormalizer().normalize(["a", "b"])
        assert all(v is None for v in lead.to_dict().values())

    def test_extra_unknown_fields_are_ignored(self) -> None:
        raw = make_lead()
        raw["future_kommo_field_xyz"] = {"nested": "stuff"}
        lead = LeadNormalizer().normalize(raw)
        assert lead.id == "25892146"

    def test_id_as_string_integer(self) -> None:
        """Some export tools serialise ids as '25892146' (string)."""
        lead = LeadNormalizer().normalize(make_lead(id="25892146"))
        assert lead.id == "25892146"

    def test_id_as_float(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(id=25892146.0))
        assert lead.id == "25892146.0"  # str() of float — acceptable

    def test_dict_id_returns_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(id={"nested": "object"}))
        assert lead.id is None


# ── Tests: normalize_many() ──────────────────────────────────────────────────

class TestNormalizeMany:
    def test_returns_list(self) -> None:
        raws = [make_lead(id=1), make_lead(id=2)]
        result = LeadNormalizer().normalize_many(raws)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_each_element_is_normalized_lead(self) -> None:
        raws = [make_lead(id=1), make_lead(id=2)]
        result = LeadNormalizer().normalize_many(raws)
        assert all(isinstance(r, NormalizedLead) for r in result)

    def test_non_iterable_returns_empty_list(self) -> None:
        result = LeadNormalizer().normalize_many(42)
        assert result == []

    def test_none_returns_empty_list(self) -> None:
        result = LeadNormalizer().normalize_many(None)
        assert result == []

    def test_empty_list_returns_empty_list(self) -> None:
        assert LeadNormalizer().normalize_many([]) == []

    def test_bad_records_do_not_interrupt_batch(self) -> None:
        raws = [make_lead(id=1), None, "bad", make_lead(id=4)]
        result = LeadNormalizer().normalize_many(raws)
        # All 4 processed — bad ones produce null leads
        assert len(result) == 4
        assert result[0].id == "1"
        assert result[1].id is None   # None → null lead
        assert result[2].id is None   # str → null lead
        assert result[3].id == "4"

    def test_stage_map_applied_to_all(self) -> None:
        raws = [make_lead(id=1, status_id=143), make_lead(id=2, status_id=96235128)]
        normalizer = LeadNormalizer(stage_map=SAMPLE_STAGE_MAP)
        result = normalizer.normalize_many(raws)
        assert result[0].status == "Leads perdidos"
        assert result[1].status == "Repesca II"


# ── Tests: determinism ───────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        raw = make_lead()
        normalizer = LeadNormalizer()
        assert normalizer.normalize(raw) == normalizer.normalize(raw)

    def test_different_instances_same_result(self) -> None:
        raw = make_lead()
        assert LeadNormalizer().normalize(raw) == LeadNormalizer().normalize(raw)

    def test_input_not_mutated(self) -> None:
        raw = make_lead()
        original = dict(raw)
        LeadNormalizer().normalize(raw)
        assert raw == original


# ── Tests: to_dict() ─────────────────────────────────────────────────────────

class TestToDict:
    def test_returns_dict(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        assert isinstance(lead.to_dict(), dict)

    def test_all_values_str_or_none(self) -> None:
        lead = LeadNormalizer().normalize(make_lead())
        for v in lead.to_dict().values():
            assert v is None or isinstance(v, str)

    def test_dict_reflects_fields(self) -> None:
        lead = LeadNormalizer().normalize(make_lead(id=999, name="Test"))
        d = lead.to_dict()
        assert d["id"] == "999"
        assert d["name"] == "Test"


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self) -> None:
        provider = KommoProvider(REAL_EXPORTS)
        self.raws = provider.get_leads()
        self.normalizer = LeadNormalizer(stage_map=provider.stages_by_id())
        self.leads = self.normalizer.normalize_many(self.raws)

    def test_count_matches(self) -> None:
        assert len(self.leads) == 448

    def test_all_are_normalized_leads(self) -> None:
        assert all(isinstance(l, NormalizedLead) for l in self.leads)

    def test_no_exceptions_raised(self) -> None:
        # If we got here, normalize_many completed without raising
        assert True

    def test_all_fields_str_or_none(self) -> None:
        for lead in self.leads:
            for field, value in lead.to_dict().items():
                assert value is None or isinstance(value, str), (
                    f"Lead {lead.id} field '{field}' is {type(value).__name__}"
                )

    def test_all_ids_present(self) -> None:
        """Real data has id on every lead."""
        assert all(l.id is not None for l in self.leads)

    def test_all_pipeline_ids_present(self) -> None:
        assert all(l.pipeline_id is not None for l in self.leads)

    def test_all_responsible_user_ids_present(self) -> None:
        assert all(l.responsible_user_id is not None for l in self.leads)

    def test_status_resolved_for_known_stages(self) -> None:
        """With stage_map, statuses should be human-readable, not raw ints."""
        non_numeric = [l for l in self.leads if l.status and not l.status.isdigit()]
        # Most statuses should resolve to stage names
        assert len(non_numeric) > 0, "Expected at least some resolved stage names"

    def test_timestamps_prefer_iso_format(self) -> None:
        """Real data has created_at_iso — all created_at should be ISO strings."""
        iso_leads = [l for l in self.leads if l.created_at and "T" in l.created_at]
        assert len(iso_leads) == 448, (
            f"Expected 448 ISO timestamps, got {len(iso_leads)}"
        )

    def test_determinism_on_real_data(self) -> None:
        """Re-normalizing the first 10 leads produces identical output."""
        second_pass = self.normalizer.normalize_many(self.raws[:10])
        assert self.leads[:10] == second_pass
