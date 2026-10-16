"""End-to-end reconciliation against the real assessment data.

EXPECTED is the mapping built by hand from reading all 42 records; the pipeline
must reproduce it exactly.
"""
from __future__ import annotations

import pytest

from event_sync.service import run_sync

EXPECTED_PAIRS = {
    "CRM-1001": ["CAL-A1"], "CRM-1002": ["CAL-A2"], "CRM-1004": ["CAL-A4"],
    "CRM-1005": ["CAL-A5", "CAL-A6"], "CRM-1006": ["CAL-A7"], "CRM-1007": ["CAL-A8"],
    "CRM-1008": ["CAL-A9"], "CRM-1009": ["CAL-A10"], "CRM-1011": ["CAL-A12"],
    "CRM-1012": ["CAL-A13"], "CRM-1013": ["CAL-A14"], "CRM-1014": ["CAL-A15"],
    "CRM-1015": ["CAL-A16"], "CRM-1016": ["CAL-A17"], "CRM-1017": ["CAL-A20"],
    "CRM-1018": ["CAL-A21"], "CRM-1019": ["CAL-A22"],
}
EXPECTED_CRM_ONLY = {"CRM-1003", "CRM-1010", "CRM-1020"}
EXPECTED_CAL_ONLY = {"CAL-A3", "CAL-A11", "CAL-A18", "CAL-A19"}


def conflict(meeting, field):
    return next((c for c in meeting["conflicts"] if c["field"] == field), None)


class TestMapping:
    def test_counts(self, result):
        s = result.stats
        assert (s["unified_meetings"], s["matched_both"], s["crm_only"], s["calendar_only"], s["duplicates_merged"]) == (24, 17, 3, 4, 1)
        assert s["failed_records"] == []

    def test_pairs_match_hand_built_table(self, result):
        got = {m["source_ids"]["crm"][0]: sorted(m["source_ids"]["calendar"]) for m in result.meetings if len(m["sources"]) == 2}
        assert got == EXPECTED_PAIRS

    def test_single_source_meetings(self, result):
        assert {m["source_ids"]["crm"][0] for m in result.meetings if m["sources"] == ["crm"]} == EXPECTED_CRM_ONLY
        assert {m["source_ids"]["calendar"][0] for m in result.meetings if m["sources"] == ["calendar"]} == EXPECTED_CAL_ONLY

    def test_every_source_record_is_used_exactly_once(self, result):
        ids = [i for m in result.meetings for i in m["source_ids"]["crm"] + m["source_ids"]["calendar"]]
        assert len(ids) == len(set(ids)) == 42

    def test_ids_are_stable_across_runs(self, result):
        assert [m["id"] for m in run_sync().meetings] == [m["id"] for m in result.meetings]

    def test_sorted_chronologically(self, result):
        keys = [(m["date"], m["fields"]["start"]["value"] or "") for m in result.meetings]
        assert keys == sorted(keys)


class TestConflicts:
    def test_utc_timestamp_conflict(self, meeting_for):
        m = meeting_for("CAL-A4")
        assert m["fields"]["start"]["value"].startswith("2025-03-13T15:00")
        assert conflict(m, "start")["severity"] == "high"

    def test_virtual_vs_in_person(self, meeting_for):
        m = meeting_for("CRM-1002")
        assert conflict(m, "format")["severity"] == "high"
        assert conflict(m, "location")["severity"] == "high"
        assert m["needs_review"]

    def test_cancellation_wins(self, meeting_for):
        m = meeting_for("CRM-1009")
        assert m["fields"]["status"]["value"] == "cancelled"
        assert m["fields"]["status"]["source_id"] == "CRM-1009"
        assert conflict(m, "status")["severity"] == "high"

    def test_two_hour_time_gap(self, meeting_for):
        assert conflict(meeting_for("CRM-1016"), "start")["severity"] == "high"

    def test_duplicate_merge_uses_latest_invite(self, meeting_for):
        m = meeting_for("CAL-A5")
        assert m["fields"]["start"]["source_id"] == "CAL-A6"
        assert m["duplicates"][0]["kept"] == "CAL-A6" and m["duplicates"][0]["merged"] == ["CAL-A5"]
        assert conflict(m, "start")["severity"] == "medium"  # 30 minutes
        emails = {p["email"] for p in m["fields"]["attendees"]["value"]}
        assert "sandra.mills@pinnaclegp.com" in emails  # union keeps the CIO from A6

    @pytest.mark.parametrize("crm_id", ["CRM-1001", "CRM-1011", "CRM-1015", "CRM-1019"])
    def test_compatible_wording_is_not_a_conflict(self, meeting_for, crm_id):
        m = meeting_for(crm_id)
        assert conflict(m, "location") is None
        assert m["fields"]["location"]["status"] in ("agreed", "complementary")

    def test_more_specific_location_is_kept(self, meeting_for):
        assert meeting_for("CRM-1001")["fields"]["location"]["value"] == "HQ - Conference Room B"

    def test_missing_crm_time_is_filled_from_calendar(self, meeting_for):
        f = meeting_for("CRM-1007")["fields"]["start"]
        assert f["source_id"] == "CAL-A8" and f["status"] == "complementary"

    def test_wording_variant_is_low(self, meeting_for):
        assert conflict(meeting_for("CRM-1017"), "location")["severity"] == "low"

    def test_owner_vs_organizer_is_low(self, meeting_for):
        assert conflict(meeting_for("CRM-1013"), "owner")["severity"] == "low"

    def test_clean_match_has_no_conflicts(self, meeting_for):
        m = meeting_for("CRM-1018")
        assert m["conflicts"] == [] and not m["needs_review"]


class TestDataQuality:
    def issues(self, meeting, source_id):
        return [i for i in meeting["data_quality"] if i["source_id"] == source_id]

    def test_malformed_date_reported(self, meeting_for):
        m = meeting_for("CRM-1008")
        assert m["date"] == "2025-03-15"
        assert any(i["field"] == "meeting_date" for i in self.issues(m, "CRM-1008"))

    def test_repaired_email_reported(self, meeting_for):
        m = meeting_for("CAL-A16")
        assert any("repaired" in i["message"] for i in self.issues(m, "CAL-A16"))
        assert "raj.patel@atlasvc.com" in {p["email"] for p in m["fields"]["attendees"]["value"]}

    def test_unresolved_attendee_kept(self, meeting_for):
        people = meeting_for("CAL-A20")["fields"]["attendees"]["value"]
        assert any(p.get("unresolved") and p["name"] == "external-guests" for p in people)

    def test_placeholder_client_replaced_by_attendees(self, meeting_for):
        f = meeting_for("CRM-1017")["fields"]["client"]
        assert f["source"] == "calendar" and "Kevin Obrien" in f["value"]

    def test_empty_calendar_event_survives(self, meeting_for):
        m = meeting_for("CAL-A11")
        assert m["fields"]["location"]["status"] == "missing"
        assert m["fields"]["end"]["value"].startswith("2025-03-14T20:00")


class TestNearMisses:
    def test_unmatched_crm_record_lists_candidate(self, meeting_for):
        near = meeting_for("CRM-1003")["possible_matches"]
        assert near and near[0]["source_ids"] == ["CAL-A7"]


class TestResilience:
    def test_one_bad_group_does_not_break_the_sync(self, monkeypatch, caplog):
        import event_sync.service as service

        original = service.build_meeting

        def flaky(crm, cal, match):
            if crm and crm.primary.source_id == "CRM-1001":
                raise RuntimeError("boom")
            return original(crm, cal, match)

        monkeypatch.setattr(service, "build_meeting", flaky)
        result = service.run_sync()
        # The matched group fails, falls back to a calendar-only meeting; CRM-1001 is reported.
        assert result.stats["failed_records"] == ["CRM-1001"]
        assert any(m["source_ids"]["calendar"] == ["CAL-A1"] for m in result.meetings)
        assert "Failed to reconcile group" in caplog.text
