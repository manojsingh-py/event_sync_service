"""Ingestion: mapping records and surviving bad input."""
from __future__ import annotations

import logging

import pytest

from event_sync.ingest import IngestError, calendar_to_record, crm_to_record, load_calendar, load_crm, load_json_array

CRM_OK = {
    "crm_id": "CRM-1", "subject": "Review", "client_name": "David Park", "client_company": "Meridian Capital",
    "relationship_owner": "Sarah Chen", "meeting_date": "2025-03-10", "meeting_time": "14:00",
    "meeting_type": "In-Person", "location": "HQ", "notes": "n", "status": "Confirmed",
    "created_at": "2025-02-28T09:15:00Z",
}
CAL_OK = {
    "event_id": "CAL-1", "title": "Review - Meridian", "organizer": "sarah.chen@firma.com",
    "attendees": ["sarah.chen@firma.com", "david.park@meridiancap.com"],
    "start_time": "2025-03-10T14:00:00", "end_time": "2025-03-10T15:00:00", "location": "HQ",
    "description": "d", "is_recurring": False, "status": "confirmed", "created_at": "2025-02-27T10:00:00Z",
}


class TestCrmMapping:
    def test_happy_path(self):
        r = crm_to_record(CRM_OK, 0)
        assert r.source_id == "CRM-1" and r.start.hour == 14
        assert r.meeting_format == "in_person" and r.category == "client"
        assert r.people == {"david park", "sarah chen"}
        assert [i for i in r.issues if i.severity != "info"] == []

    def test_internal_meeting_without_client(self):
        r = crm_to_record({**CRM_OK, "client_name": None, "client_company": None, "meeting_type": "Internal"}, 0)
        assert r.category == "internal" and r.meeting_format is None

    def test_missing_time_keeps_date(self):
        r = crm_to_record({**CRM_OK, "meeting_time": None}, 0)
        assert r.start is None and r.local_date.isoformat() == "2025-03-10"

    def test_missing_id_gets_synthetic_id(self):
        r = crm_to_record({**CRM_OK, "crm_id": None}, 7)
        assert r.source_id == "CRM-MISSING-7"
        assert any(i.severity == "error" for i in r.issues)

    def test_placeholder_client_is_not_a_person(self):
        r = crm_to_record({**CRM_OK, "client_name": "Multiple"}, 0)
        assert "multiple" not in r.people


class TestCalendarMapping:
    def test_happy_path(self):
        r = calendar_to_record(CAL_OK, 0)
        assert r.category == "client" and r.org_keys == {"meridiancap"}
        assert r.client_name == "David Park"
        assert "sarah chen" in r.people

    def test_no_attendees_means_unknown_category(self):
        r = calendar_to_record({**CAL_OK, "attendees": []}, 0)
        assert r.category is None  # only the organizer is known

    def test_end_before_start_is_flagged(self):
        r = calendar_to_record({**CAL_OK, "end_time": "2025-03-10T13:00:00"}, 0)
        assert any(i.field == "end_time" and "not after" in i.message for i in r.issues)

    def test_garbage_fields_do_not_crash(self):
        r = calendar_to_record({"event_id": "CAL-X", "start_time": 5, "attendees": "nope", "is_recurring": "yes"}, 0)
        assert r.start is None and r.attendees == [] and r.is_recurring is None
        assert any(i.severity == "error" for i in r.issues)


class TestLoaders:
    def test_missing_file(self, tmp_path):
        with pytest.raises(IngestError, match="not found"):
            load_json_array(tmp_path / "nope.json")

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(IngestError, match="Invalid JSON"):
            load_json_array(p)

    def test_not_an_array(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        with pytest.raises(IngestError, match="JSON array"):
            load_json_array(p)

    def test_non_object_entries_are_dropped(self, tmp_path, caplog):
        p = tmp_path / "mixed.json"
        p.write_text('[1, "x", {"crm_id": "CRM-1"}]', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="event_sync"):
            assert len(load_json_array(p)) == 1
        assert "non-object" in caplog.text

    def test_unmappable_record_is_skipped_and_logged(self, write_sources, monkeypatch, caplog):
        crm_path, cal_path = write_sources([CRM_OK, {**CRM_OK, "crm_id": "CRM-BOOM"}], [CAL_OK])

        import event_sync.ingest as ingest

        original = ingest.crm_to_record

        def flaky(raw, index):
            if raw.get("crm_id") == "CRM-BOOM":
                raise RuntimeError("boom")
            return original(raw, index)

        monkeypatch.setattr(ingest, "crm_to_record", flaky)
        with caplog.at_level(logging.ERROR, logger="event_sync"):
            records = load_crm(crm_path)
        assert [r.source_id for r in records] == ["CRM-1"]
        assert "could not be mapped" in caplog.text

    def test_real_files_load(self):
        from event_sync import config

        assert len(load_crm(config.CRM_FILE)) == 20
        assert len(load_calendar(config.CALENDAR_FILE)) == 22
