"""
Tests for KommoExportService.

Test groups:
  TestInit              — construction, repr
  TestPreview           — count-only preview dict
  TestToRecords         — in-memory record generation
  TestRecordSchema      — JSONL record field contracts
  TestMetricsInRecord   — metrics sub-dict
  TestExportFile        — disk write, checksum, size
  TestStream            — generator yields valid lines
  TestFilters           — include_kommo_only / include_rinkel_only / min_score
  TestEdgeCases         — empty dir, bad agent, no calls
  TestBuildBatchPrompt  — Claude prompt integration
  TestRealData          — smoke tests against 448 real leads
"""

from __future__ import annotations

import json
import hashlib
import pytest
from pathlib import Path

from app.services.kommo_export_service import KommoExportService, EXPORT_SCHEMA


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_LEADS = [
    {
        "id": 1001, "name": "Lead Alpha", "pipeline_id": 200,
        "status_id": 142, "responsible_user_id": 5001,
        "created_at": 1748390400, "updated_at": 1748476800,
        "created_at_iso": "2026-05-28T00:00:00+00:00",
        "updated_at_iso": "2026-05-29T00:00:00+00:00",
        "loss_reason_id": None, "closed_at": None,
        "custom_fields_values": [],
    },
    {
        "id": 1002, "name": "Lead Beta", "pipeline_id": 200,
        "status_id": 143, "responsible_user_id": 5001,
        "created_at": 1748304000, "updated_at": 1748390400,
        "created_at_iso": "2026-05-27T00:00:00+00:00",
        "updated_at_iso": "2026-05-28T00:00:00+00:00",
        "loss_reason_id": 55, "closed_at": 1748390400,  # lost
        "custom_fields_values": [],
    },
    {
        "id": 1003, "name": "Lead Gamma", "pipeline_id": 201,
        "status_id": 300, "responsible_user_id": 5002,
        "created_at": 1748217600, "updated_at": 1748304000,
        "created_at_iso": "2026-05-26T00:00:00+00:00",
        "updated_at_iso": "2026-05-27T00:00:00+00:00",
        "loss_reason_id": None, "closed_at": 1748217600,  # won
        "custom_fields_values": [],
    },
]

SAMPLE_PIPELINES = [
    {
        "pipeline_id": 200, "pipeline_name": "Klantenservice",
        "sort": 1, "is_main": True, "is_archive": False,
        "account_id": 99001, "total_stages": 3, "regular_stages": 2,
        "stages": [
            {"stage_id": 141, "stage_name": "Nieuw",          "pipeline_id": 200, "sort": 10, "color": "#fff", "is_editable": True},
            {"stage_id": 142, "stage_name": "Leads ganados",  "pipeline_id": 200, "sort": 20, "color": "#0f0", "is_editable": False},
            {"stage_id": 143, "stage_name": "Leads perdidos", "pipeline_id": 200, "sort": 30, "color": "#f00", "is_editable": False},
        ],
    },
    {
        "pipeline_id": 201, "pipeline_name": "Creditering",
        "sort": 2, "is_main": False, "is_archive": False,
        "account_id": 99001, "total_stages": 2, "regular_stages": 2,
        "stages": [
            {"stage_id": 300, "stage_name": "Aanvraag",    "pipeline_id": 201, "sort": 10, "color": "#ccc", "is_editable": True},
            {"stage_id": 301, "stage_name": "Goedgekeurd", "pipeline_id": 201, "sort": 20, "color": "#0f0", "is_editable": True},
        ],
    },
]

SAMPLE_RINKEL_CALLS = [
    {"call_id": "CALL-001", "agent_id": "5001", "direction": "inbound",  "duration": 120},
    {"call_id": "CALL-002", "agent_id": "5001", "direction": "outbound", "duration": 60},
    {"call_id": "CALL-003", "agent_id": "9999", "direction": "inbound",  "duration": 45},
]


def _write(directory: Path, name: str, data: object) -> None:
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


def _make_exporter(tmp_path: Path, **kwargs) -> KommoExportService:
    _write(tmp_path, "leads.json",     {"_meta": {"entity": "leads", "count": 3,
                                                    "extracted_at": "2026-05-28T06:00:00Z",
                                                    "source": "kommo_api_v4"},
                                        "data": SAMPLE_LEADS})
    _write(tmp_path, "pipelines.json", {"_meta": {"entity": "pipelines", "count": 2,
                                                    "total_stages": 5,
                                                    "extracted_at": "2026-05-28T06:00:00Z",
                                                    "source": "kommo_api_v4"},
                                        "data": SAMPLE_PIPELINES})
    output_dir = tmp_path / "jsonl"
    return KommoExportService(
        exports_dir=str(tmp_path),
        output_dir=str(output_dir),
        **kwargs,
    )


# ── Tests: init ───────────────────────────────────────────────────────────────

class TestInit:
    def test_construction_no_crash(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc is not None

    def test_repr_contains_output_dir(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert "jsonl" in repr(svc)

    def test_default_construction(self) -> None:
        svc = KommoExportService()
        assert svc is not None


# ── Tests: preview ────────────────────────────────────────────────────────────

class TestPreview:
    def test_returns_dict(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert isinstance(svc.preview(), dict)

    def test_required_keys(self, tmp_path: Path) -> None:
        p = _make_exporter(tmp_path).preview()
        assert set(p.keys()) >= {
            "total_agents", "total_leads", "total_calls",
            "matched_agents", "kommo_only_agents", "rinkel_only_agents",
            "estimated_size_bytes", "filters",
        }

    def test_total_agents_without_calls(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc.preview()["total_agents"] == 2  # 5001 and 5002

    def test_total_leads(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc.preview()["total_leads"] == 3

    def test_total_calls_zero_without_rinkel(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc.preview()["total_calls"] == 0

    def test_total_calls_with_rinkel(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        # 2 calls for 5001, 1 call for 9999
        assert svc.preview()["total_calls"] == 3

    def test_matched_agents_with_rinkel(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        assert svc.preview()["matched_agents"] >= 1

    def test_estimated_size_non_zero(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc.preview()["estimated_size_bytes"] > 0

    def test_filters_in_preview(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, min_engagement_score=0.5)
        f = svc.preview()["filters"]
        assert f["min_engagement_score"] == 0.5

    def test_empty_dir_preview(self, tmp_path: Path) -> None:
        svc = KommoExportService(
            exports_dir=str(tmp_path),
            output_dir=str(tmp_path / "jsonl"),
        )
        p = svc.preview()
        assert p["total_agents"] == 0
        assert p["total_leads"] == 0


# ── Tests: to_records() ──────────────────────────────────────────────────────

class TestToRecords:
    def test_returns_list(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert isinstance(svc.to_records(), list)

    def test_one_record_per_agent(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert len(svc.to_records()) == 2

    def test_each_record_is_dict(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        for r in svc.to_records():
            assert isinstance(r, dict)

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        svc = KommoExportService(
            exports_dir=str(tmp_path),
            output_dir=str(tmp_path / "jsonl"),
        )
        assert svc.to_records() == []


# ── Tests: record schema ──────────────────────────────────────────────────────

class TestRecordSchema:
    def setup_method(self, tmp_path_factory) -> None:
        pass

    def _get_record(self, tmp_path: Path) -> dict:
        svc = _make_exporter(tmp_path)
        records = svc.to_records()
        return next(r for r in records if r["agent_id"] == "5001")

    def test_agent_id_is_str(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert isinstance(r["agent_id"], str)

    def test_export_schema_field(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert r["export_schema"] == EXPORT_SCHEMA

    def test_exported_at_is_iso(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert "T" in r["exported_at"]

    def test_metrics_present(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert isinstance(r["metrics"], dict)

    def test_normalized_leads_present(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert isinstance(r["normalized_leads"], list)

    def test_rinkel_calls_present(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert isinstance(r["rinkel_calls"], list)

    def test_pipeline_summary_present(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert isinstance(r["pipeline_summary"], list)

    def test_normalized_leads_count(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        # Agent 5001 has 2 leads
        assert len(r["normalized_leads"]) == 2

    def test_normalized_leads_are_str_or_none(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        for lead in r["normalized_leads"]:
            for v in lead.values():
                assert v is None or isinstance(v, str)

    def test_pipeline_summary_has_name(self, tmp_path: Path) -> None:
        r = self._get_record(tmp_path)
        assert r["pipeline_summary"][0]["pipeline_name"] == "Klantenservice"

    def test_json_serialisable(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        for r in svc.to_records():
            json.dumps(r)  # must not raise


# ── Tests: metrics sub-dict ───────────────────────────────────────────────────

class TestMetricsInRecord:
    def _record(self, tmp_path: Path) -> dict:
        svc = _make_exporter(tmp_path)
        return next(r for r in svc.to_records() if r["agent_id"] == "5001")["metrics"]

    def test_all_metric_keys_present(self, tmp_path: Path) -> None:
        m = self._record(tmp_path)
        expected = {
            "agent_id", "total_leads", "converted_leads", "lost_leads",
            "active_leads", "conversion_rate", "total_calls",
            "avg_call_duration", "inbound_calls", "outbound_calls",
            "leads_to_calls_ratio", "call_coverage_rate",
            "activity_consistency", "responsiveness_proxy", "engagement_score",
        }
        assert expected.issubset(m.keys())

    def test_total_leads_correct(self, tmp_path: Path) -> None:
        m = self._record(tmp_path)
        assert m["total_leads"] == 2

    def test_lost_leads_detected(self, tmp_path: Path) -> None:
        # Lead 1002 has loss_reason_id=55 → lost
        m = self._record(tmp_path)
        assert m["lost_leads"] >= 1

    def test_calls_zero_without_rinkel(self, tmp_path: Path) -> None:
        m = self._record(tmp_path)
        assert m["total_calls"] == 0

    def test_calls_present_with_rinkel(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        r = next(r for r in svc.to_records() if r["agent_id"] == "5001")
        assert r["metrics"]["total_calls"] == 2

    def test_engagement_score_in_range(self, tmp_path: Path) -> None:
        m = self._record(tmp_path)
        assert 0.0 <= m["engagement_score"] <= 1.0


# ── Tests: export file ────────────────────────────────────────────────────────

class TestExportFile:
    def test_returns_dict(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        result = svc.export()
        assert isinstance(result, dict)

    def test_required_keys(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert set(result.keys()) >= {
            "file_path", "records_written", "total_leads",
            "total_calls", "file_size_bytes", "checksum",
            "duration_ms", "exported_at",
        }

    def test_file_created(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert Path(result["file_path"]).exists()

    def test_records_written_count(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert result["records_written"] == 2

    def test_file_is_valid_jsonl(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        lines = Path(result["file_path"]).read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must not raise

    def test_checksum_is_sha256(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert len(result["checksum"]) == 64  # SHA-256 hex

    def test_checksum_matches_file(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        content = Path(result["file_path"]).read_bytes()
        expected = hashlib.sha256(content).hexdigest()
        assert result["checksum"] == expected

    def test_file_size_accurate(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        actual = Path(result["file_path"]).stat().st_size
        assert result["file_size_bytes"] == actual

    def test_total_leads_in_result(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert result["total_leads"] == 3

    def test_duration_ms_non_negative(self, tmp_path: Path) -> None:
        result = _make_exporter(tmp_path).export()
        assert result["duration_ms"] >= 0

    def test_custom_filename(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        result = svc.export(filename="custom_name.jsonl")
        assert "custom_name.jsonl" in result["file_path"]

    def test_output_dir_created(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "deep" / "nested" / "jsonl"
        svc = KommoExportService(
            exports_dir=str(tmp_path),
            output_dir=str(output_dir),
        )
        # Write the files first
        import json as _json
        (tmp_path / "leads.json").write_text(_json.dumps({"_meta": {}, "data": []}))
        (tmp_path / "pipelines.json").write_text(_json.dumps({"_meta": {}, "data": []}))
        svc.export()
        assert output_dir.exists()


# ── Tests: stream() ──────────────────────────────────────────────────────────

class TestStream:
    def test_yields_strings(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        for line in svc.stream():
            assert isinstance(line, str)

    def test_each_line_is_valid_json(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        for line in svc.stream():
            json.loads(line)

    def test_line_ends_with_newline(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        for line in svc.stream():
            assert line.endswith("\n")

    def test_line_count(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        lines = list(svc.stream())
        assert len(lines) == 2

    def test_stream_consistent_with_to_records(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        from_records = svc.to_records()
        from_stream  = [json.loads(l) for l in svc.stream()]
        assert {r["agent_id"] for r in from_records} == {r["agent_id"] for r in from_stream}


# ── Tests: filters ────────────────────────────────────────────────────────────

class TestFilters:
    def test_exclude_kommo_only(self, tmp_path: Path) -> None:
        """Without Rinkel calls, all agents are Kommo-only → all excluded."""
        svc = _make_exporter(tmp_path, include_kommo_only=False)
        assert len(svc.to_records()) == 0

    def test_exclude_rinkel_only(self, tmp_path: Path) -> None:
        """Agent 9999 is Rinkel-only → excluded when include_rinkel_only=False."""
        svc = _make_exporter(
            tmp_path,
            rinkel_calls=SAMPLE_RINKEL_CALLS,
            include_rinkel_only=False,
        )
        ids = {r["agent_id"] for r in svc.to_records()}
        assert "9999" not in ids

    def test_include_rinkel_only(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        ids = {r["agent_id"] for r in svc.to_records()}
        assert "9999" in ids

    def test_min_engagement_score_filters_low_agents(self, tmp_path: Path) -> None:
        """min_score=0.99 should filter out most agents."""
        svc = _make_exporter(tmp_path, min_engagement_score=0.99)
        # Very high threshold — likely 0 agents pass
        records = svc.to_records()
        for r in records:
            assert r["metrics"]["engagement_score"] >= 0.99

    def test_min_engagement_score_zero_keeps_all(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, min_engagement_score=0.0)
        assert len(svc.to_records()) == 2


# ── Tests: edge cases ─────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_dir_no_crash(self, tmp_path: Path) -> None:
        svc = KommoExportService(
            exports_dir=str(tmp_path),
            output_dir=str(tmp_path / "jsonl"),
        )
        assert svc.to_records() == []
        result = svc.export()
        assert result["records_written"] == 0

    def test_empty_export_file_created(self, tmp_path: Path) -> None:
        svc = KommoExportService(
            exports_dir=str(tmp_path),
            output_dir=str(tmp_path / "jsonl"),
        )
        result = svc.export()
        assert Path(result["file_path"]).exists()

    def test_with_explicit_agent_id_map(self, tmp_path: Path) -> None:
        calls = [{"call_id": "C1", "agent_id": "sophie"}]
        svc = _make_exporter(
            tmp_path,
            rinkel_calls=calls,
            agent_id_map={"sophie": "5001"},
        )
        records = svc.to_records()
        r5001 = next((r for r in records if r["agent_id"] == "5001"), None)
        assert r5001 is not None
        assert r5001["metrics"]["total_calls"] == 1


# ── Tests: batch prompt integration ──────────────────────────────────────────

class TestBuildBatchPrompt:
    def test_returns_none_for_unknown_agent(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        assert svc.build_batch_prompt("99999") is None

    def test_returns_none_when_no_calls(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path)
        # Agent 5001 has no Rinkel calls
        result = svc.build_batch_prompt("5001")
        assert result is None

    def test_returns_tuple_when_calls_present(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        result = svc.build_batch_prompt("5001")
        assert result is not None
        system, user = result
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str) and len(user) > 0

    def test_prompt_contains_agent_id(self, tmp_path: Path) -> None:
        svc = _make_exporter(tmp_path, rinkel_calls=SAMPLE_RINKEL_CALLS)
        _, user = svc.build_batch_prompt("5001")
        assert "5001" in user


# ── Tests: real data smoke tests ─────────────────────────────────────────────

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "exports"

@pytest.mark.skipif(
    not (REAL_EXPORTS / "leads.json").exists(),
    reason="Real exports/leads.json not present",
)
class TestRealData:
    def setup_method(self, tmp_path_factory=None) -> None:
        self.output_dir = REAL_EXPORTS / "jsonl_test"
        self.svc = KommoExportService(
            exports_dir=str(REAL_EXPORTS),
            output_dir=str(self.output_dir),
        )

    def teardown_method(self) -> None:
        # Clean up generated test files
        import shutil
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_preview_448_leads(self) -> None:
        p = self.svc.preview()
        assert p["total_leads"] == 448

    def test_to_records_count(self) -> None:
        records = self.svc.to_records()
        assert len(records) >= 1

    def test_all_records_are_dicts(self) -> None:
        for r in self.svc.to_records():
            assert isinstance(r, dict)

    def test_all_records_json_serialisable(self) -> None:
        for r in self.svc.to_records():
            json.dumps(r)

    def test_total_leads_preserved(self) -> None:
        total = sum(r["metrics"]["total_leads"] for r in self.svc.to_records())
        assert total == 448

    def test_all_engagement_scores_in_range(self) -> None:
        for r in self.svc.to_records():
            s = r["metrics"]["engagement_score"]
            assert 0.0 <= s <= 1.0

    def test_export_file_written(self) -> None:
        result = self.svc.export()
        assert Path(result["file_path"]).exists()
        assert result["records_written"] >= 1
        assert result["total_leads"] == 448

    def test_checksum_validates(self) -> None:
        result = self.svc.export()
        content = Path(result["file_path"]).read_bytes()
        expected = __import__("hashlib").sha256(content).hexdigest()
        assert result["checksum"] == expected

    def test_stream_line_count(self) -> None:
        records  = self.svc.to_records()
        streamed = list(self.svc.stream())
        assert len(streamed) == len(records)

    def test_all_normalized_leads_str_or_none(self) -> None:
        for r in self.svc.to_records():
            for lead in r["normalized_leads"]:
                for v in lead.values():
                    assert v is None or isinstance(v, str)
