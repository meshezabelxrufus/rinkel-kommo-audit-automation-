"""
Tests for KommoProvider — read-only local JSON provider.

Covers:
    - get_leads()         with valid data
    - get_pipelines()     with valid data
    - get_contacts()      optional file present
    - get_users()         optional file absent
    - missing required file returns [] (no crash)
    - malformed JSON returns [] (no crash)
    - empty list file returns []
    - Kommo v4 _embedded envelope is unwrapped correctly
    - leads_by_id()                     lookup helper
    - leads_by_responsible_user()       core join helper
    - pipelines_by_id()                 lookup helper
    - available_files()                 introspection
    - custom exports_dir path
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from app.integrations.kommo import KommoProvider


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_LEADS = [
    {
        "id": 1001,
        "name": "Storing melding - Maria Jansen",
        "responsible_user_id": 5001,
        "status_id": 142,
        "pipeline_id": 200,
        "created_at": 1748390400,
        "updated_at": 1748476800,
        "custom_fields_values": [],
    },
    {
        "id": 1002,
        "name": "Facturering - Pieter Bakker",
        "responsible_user_id": 5002,
        "status_id": 143,
        "pipeline_id": 200,
        "created_at": 1748304000,
        "updated_at": 1748390400,
        "custom_fields_values": [],
    },
    {
        "id": 1003,
        "name": "Credit - Hans de Boer",
        "responsible_user_id": 5002,
        "status_id": 142,
        "pipeline_id": 201,
        "created_at": 1748217600,
        "updated_at": 1748304000,
        "custom_fields_values": [],
    },
]

SAMPLE_PIPELINES = [
    {
        "id": 200,
        "name": "Klantenservice",
        "_embedded": {
            "statuses": [
                {"id": 141, "name": "Nieuwe melding", "pipeline_id": 200},
                {"id": 142, "name": "In behandeling", "pipeline_id": 200},
                {"id": 143, "name": "Opgelost", "pipeline_id": 200},
            ]
        },
    },
    {
        "id": 201,
        "name": "Creditering",
        "_embedded": {
            "statuses": [
                {"id": 210, "name": "Aanvraag ontvangen", "pipeline_id": 201},
                {"id": 211, "name": "Verificatie", "pipeline_id": 201},
            ]
        },
    },
]

SAMPLE_CONTACTS = [
    {"id": 3001, "name": "Maria Jansen", "responsible_user_id": 5001},
    {"id": 3002, "name": "Pieter Bakker", "responsible_user_id": 5002},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write(directory: Path, filename: str, data: object) -> None:
    """Write data as JSON to directory/filename."""
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


# ── Tests: get_leads() ────────────────────────────────────────────────────────

class TestGetLeads:
    def test_returns_list_of_dicts(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        provider = KommoProvider(tmp_path)
        leads = provider.get_leads()
        assert isinstance(leads, list)
        assert len(leads) == 3

    def test_lead_shape_has_required_fields(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        lead = KommoProvider(tmp_path).get_leads()[0]
        assert lead["id"] == 1001
        assert lead["responsible_user_id"] == 5001
        assert lead["pipeline_id"] == 200

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        # No leads.json in directory
        provider = KommoProvider(tmp_path)
        result = provider.get_leads()
        assert result == []

    def test_malformed_json_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "leads.json").write_text("{not valid json", encoding="utf-8")
        result = KommoProvider(tmp_path).get_leads()
        assert result == []

    def test_empty_list_file_returns_empty_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", [])
        result = KommoProvider(tmp_path).get_leads()
        assert result == []

    def test_v4_embedded_envelope_is_unwrapped(self, tmp_path: Path) -> None:
        """Kommo v4 API wraps leads in {"_embedded": {"leads": [...]}}."""
        envelope = {"_embedded": {"leads": SAMPLE_LEADS}}
        _write(tmp_path, "leads.json", envelope)
        result = KommoProvider(tmp_path).get_leads()
        assert len(result) == 3
        assert result[0]["id"] == 1001

    def test_top_level_key_envelope_is_unwrapped(self, tmp_path: Path) -> None:
        """Some exports use {"leads": [...]} without _embedded."""
        envelope = {"leads": SAMPLE_LEADS}
        _write(tmp_path, "leads.json", envelope)
        result = KommoProvider(tmp_path).get_leads()
        assert len(result) == 3

    def test_raw_data_is_unchanged(self, tmp_path: Path) -> None:
        """Provider must return raw data without any transformation."""
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        leads = KommoProvider(tmp_path).get_leads()
        assert leads[1]["name"] == "Facturering - Pieter Bakker"
        assert leads[1]["custom_fields_values"] == []


# ── Tests: get_pipelines() ────────────────────────────────────────────────────

class TestGetPipelines:
    def test_returns_list_of_dicts(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", SAMPLE_PIPELINES)
        pipelines = KommoProvider(tmp_path).get_pipelines()
        assert isinstance(pipelines, list)
        assert len(pipelines) == 2

    def test_pipeline_shape(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", SAMPLE_PIPELINES)
        p = KommoProvider(tmp_path).get_pipelines()[0]
        assert p["id"] == 200
        assert p["name"] == "Klantenservice"
        assert len(p["_embedded"]["statuses"]) == 3

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        result = KommoProvider(tmp_path).get_pipelines()
        assert result == []

    def test_malformed_json_returns_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "pipelines.json").write_text("<<<", encoding="utf-8")
        result = KommoProvider(tmp_path).get_pipelines()
        assert result == []

    def test_v4_embedded_envelope_is_unwrapped(self, tmp_path: Path) -> None:
        envelope = {"_embedded": {"pipelines": SAMPLE_PIPELINES}}
        _write(tmp_path, "pipelines.json", envelope)
        result = KommoProvider(tmp_path).get_pipelines()
        assert len(result) == 2


# ── Tests: get_contacts() (optional) ─────────────────────────────────────────

class TestGetContacts:
    def test_returns_contacts_when_file_present(self, tmp_path: Path) -> None:
        _write(tmp_path, "contacts.json", SAMPLE_CONTACTS)
        contacts = KommoProvider(tmp_path).get_contacts()
        assert len(contacts) == 2
        assert contacts[0]["name"] == "Maria Jansen"

    def test_returns_empty_list_when_file_absent(self, tmp_path: Path) -> None:
        """contacts.json is optional — must not crash when missing."""
        result = KommoProvider(tmp_path).get_contacts()
        assert result == []

    def test_returns_empty_list_when_malformed(self, tmp_path: Path) -> None:
        (tmp_path / "contacts.json").write_text("null", encoding="utf-8")
        result = KommoProvider(tmp_path).get_contacts()
        # null parses to None which is not a list/dict → returns []
        assert result == []


# ── Tests: get_users() (optional) ────────────────────────────────────────────

class TestGetUsers:
    def test_returns_empty_list_when_file_absent(self, tmp_path: Path) -> None:
        """users.json is optional — must not crash when missing."""
        result = KommoProvider(tmp_path).get_users()
        assert result == []

    def test_returns_users_when_file_present(self, tmp_path: Path) -> None:
        users = [
            {"id": 5001, "name": "Sophie van Dijk", "email": "sophie@company.nl"},
            {"id": 5002, "name": "Jan de Vries", "email": "jan@company.nl"},
        ]
        _write(tmp_path, "users.json", users)
        result = KommoProvider(tmp_path).get_users()
        assert len(result) == 2
        assert result[0]["id"] == 5001


# ── Tests: lookup helpers ─────────────────────────────────────────────────────

class TestLeadsById:
    def test_returns_dict_keyed_by_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        mapping = KommoProvider(tmp_path).leads_by_id()
        assert isinstance(mapping, dict)
        assert 1001 in mapping
        assert mapping[1001]["name"] == "Storing melding - Maria Jansen"

    def test_all_leads_indexed(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        mapping = KommoProvider(tmp_path).leads_by_id()
        assert set(mapping.keys()) == {1001, 1002, 1003}

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).leads_by_id() == {}


class TestLeadsByResponsibleUser:
    """Core join helper — maps responsible_user_id → leads."""

    def test_groups_leads_by_agent(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        mapping = KommoProvider(tmp_path).leads_by_responsible_user()
        # user 5001 has 1 lead; user 5002 has 2 leads
        assert len(mapping[5001]) == 1
        assert len(mapping[5002]) == 2

    def test_lead_ids_under_agent(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        mapping = KommoProvider(tmp_path).leads_by_responsible_user()
        lead_ids_5002 = {l["id"] for l in mapping[5002]}
        assert lead_ids_5002 == {1002, 1003}

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).leads_by_responsible_user() == {}


class TestPipelinesById:
    def test_returns_dict_keyed_by_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "pipelines.json", SAMPLE_PIPELINES)
        mapping = KommoProvider(tmp_path).pipelines_by_id()
        assert 200 in mapping
        assert mapping[200]["name"] == "Klantenservice"

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).pipelines_by_id() == {}


class TestUsersById:
    def test_returns_dict_keyed_by_id(self, tmp_path: Path) -> None:
        users = [{"id": 5001, "name": "Sophie"}, {"id": 5002, "name": "Jan"}]
        _write(tmp_path, "users.json", users)
        mapping = KommoProvider(tmp_path).users_by_id()
        assert mapping[5001]["name"] == "Sophie"
        assert mapping[5002]["name"] == "Jan"

    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        assert KommoProvider(tmp_path).users_by_id() == {}


# ── Tests: available_files() ──────────────────────────────────────────────────

class TestAvailableFiles:
    def test_all_absent_in_empty_dir(self, tmp_path: Path) -> None:
        result = KommoProvider(tmp_path).available_files()
        assert result == {
            "leads.json": False,
            "pipelines.json": False,
            "contacts.json": False,
            "users.json": False,
        }

    def test_detects_present_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        _write(tmp_path, "pipelines.json", SAMPLE_PIPELINES)
        result = KommoProvider(tmp_path).available_files()
        assert result["leads.json"] is True
        assert result["pipelines.json"] is True
        assert result["contacts.json"] is False
        assert result["users.json"] is False


# ── Tests: robustness / edge cases ────────────────────────────────────────────

class TestEdgeCases:
    def test_nonexistent_exports_dir_does_not_crash(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does_not_exist"
        provider = KommoProvider(ghost)
        assert provider.get_leads() == []
        assert provider.get_pipelines() == []
        assert provider.get_contacts() == []
        assert provider.get_users() == []

    def test_multiple_calls_are_idempotent(self, tmp_path: Path) -> None:
        _write(tmp_path, "leads.json", SAMPLE_LEADS)
        provider = KommoProvider(tmp_path)
        assert provider.get_leads() == provider.get_leads()

    def test_repr_contains_path(self, tmp_path: Path) -> None:
        provider = KommoProvider(tmp_path)
        assert str(tmp_path) in repr(provider)

    def test_exports_dir_returns_path(self, tmp_path: Path) -> None:
        provider = KommoProvider(tmp_path)
        assert provider.exports_dir() == tmp_path

    def test_unicode_content_is_preserved(self, tmp_path: Path) -> None:
        """Dutch characters must survive the round-trip."""
        leads = [{"id": 9999, "name": "Monteur inplannen — Dë Vries", "responsible_user_id": 1}]
        _write(tmp_path, "leads.json", leads)
        result = KommoProvider(tmp_path).get_leads()
        assert result[0]["name"] == "Monteur inplannen — Dë Vries"

    def test_lead_missing_id_excluded_from_lookup(self, tmp_path: Path) -> None:
        """leads_by_id() must skip leads without a valid integer id."""
        leads = [
            {"id": 100, "responsible_user_id": 5001},
            {"name": "no id field", "responsible_user_id": 5001},  # no id
            {"id": None, "responsible_user_id": 5001},              # null id
        ]
        _write(tmp_path, "leads.json", leads)
        mapping = KommoProvider(tmp_path).leads_by_id()
        assert set(mapping.keys()) == {100}


# ── Tests: default path resolution ───────────────────────────────────────────

class TestDefaultPath:
    def test_default_exports_dir_is_resolved(self) -> None:
        """Without a custom path, the provider should resolve to .../rinkel/exports."""
        provider = KommoProvider()
        path = provider.exports_dir()
        assert path.name == "exports"
        assert path.is_absolute()
