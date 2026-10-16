"""Unit tests for parsing and similarity helpers."""
from __future__ import annotations

from datetime import date, time

import pytest

from event_sync.normalize import (
    clean_attendees,
    company_matches_domain,
    compare_locations,
    location_kind,
    name_from_email,
    name_key,
    normalise_format,
    parse_crm_date,
    parse_crm_time,
    parse_timestamp,
    text_similarity,
    tokens,
)


def severities(issues):
    return [i.severity for i in issues]


class TestTimestamps:
    def test_naive_is_interpreted_as_eastern(self):
        issues = []
        dt = parse_timestamp("2025-03-10T14:00:00", "start_time", issues)
        assert dt.hour == 14 and dt.utcoffset().total_seconds() == -4 * 3600  # EDT after 9 Mar
        assert issues == []

    def test_utc_is_converted_with_dst(self):
        issues = []
        dt = parse_timestamp("2025-03-13T19:00:00Z", "start_time", issues, note_explicit_tz=True)
        assert (dt.hour, dt.minute) == (15, 0)  # not 14:00 - DST already started
        assert severities(issues) == ["info"]

    def test_before_dst_uses_standard_offset(self):
        dt = parse_timestamp("2025-03-01T19:00:00Z", "x", [])
        assert dt.hour == 14

    def test_missing_seconds_is_parsed_with_warning(self):
        issues = []
        dt = parse_timestamp("2025-03-14T20:00", "end_time", issues)
        assert dt.hour == 20
        assert severities(issues) == ["warning"]

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing(self, value):
        issues = []
        assert parse_timestamp(value, "x", issues) is None
        assert severities(issues) == ["warning"]

    @pytest.mark.parametrize("value", ["not a date", "2025-13-45T99:00:00", 12345])
    def test_unparseable_is_an_error_not_a_crash(self, value):
        issues = []
        assert parse_timestamp(value, "x", issues) is None
        assert severities(issues) == ["error"]


class TestCrmDateTime:
    def test_iso_date(self):
        issues = []
        assert parse_crm_date("2025-03-10", issues) == date(2025, 3, 10)
        assert issues == []

    def test_malformed_date_from_data(self):
        issues = []
        assert parse_crm_date("03-15/2025", issues) == date(2025, 3, 15)
        assert severities(issues) == ["warning"]

    @pytest.mark.parametrize("value", [None, "garbage", "2025-02-30"])
    def test_bad_dates(self, value):
        issues = []
        assert parse_crm_date(value, issues) is None
        assert severities(issues) == ["error"]

    def test_time_formats(self):
        assert parse_crm_time("14:00", []) == time(14, 0)
        issues = []
        assert parse_crm_time("2:30 pm", issues) == time(14, 30)
        assert severities(issues) == ["warning"]

    def test_missing_time_is_a_warning(self):
        issues = []
        assert parse_crm_time(None, issues) is None
        assert severities(issues) == ["warning"]


class TestAttendees:
    def test_valid_and_dedup(self):
        valid, unresolved = clean_attendees(["A.B@x.com", "a.b@x.com"], [])
        assert valid == ["a.b@x.com"] and unresolved == []

    def test_obfuscated_email_is_repaired(self):
        issues = []
        valid, _ = clean_attendees(["raj.patel[at]atlasvc.com"], issues)
        assert valid == ["raj.patel@atlasvc.com"]
        assert "repaired" in issues[0].message

    def test_non_email_is_kept_as_unresolved(self):
        valid, unresolved = clean_attendees(["external-guests"], [])
        assert valid == [] and unresolved == ["external-guests"]

    @pytest.mark.parametrize("raw, severity", [([], "warning"), (None, "warning"), ("a@b.com", "error")])
    def test_empty_missing_or_wrong_type(self, raw, severity):
        issues = []
        assert clean_attendees(raw, issues) == ([], [])
        assert issues[0].severity == severity

    def test_blank_entries_ignored(self):
        valid, _ = clean_attendees(["", None, "a@b.com"], [])
        assert valid == ["a@b.com"]


class TestNamesAndOrgs:
    def test_name_key_matches_email_local_part(self):
        assert name_key("Kevin O'Brien") == name_from_email("kevin.obrien@pinnaclegp.com")

    @pytest.mark.parametrize("value", ["Multiple", "TBD", "", None])
    def test_placeholders_are_not_names(self, value):
        assert name_key(value) is None

    @pytest.mark.parametrize("company, domain", [
        ("Meridian Capital", "meridiancap.com"),
        ("Atlas Ventures", "atlasvc.com"),
        ("Pinnacle Group", "pinnaclegp.com"),
        ("Horizon Wealth Partners", "horizonwp.com"),
    ])
    def test_company_matches_abbreviated_domain(self, company, domain):
        assert company_matches_domain(company, domain)

    def test_company_does_not_match_unrelated_domain(self):
        assert not company_matches_domain("Meridian Capital", "summitadv.com")
        assert not company_matches_domain(None, "summitadv.com")


class TestLocations:
    @pytest.mark.parametrize("value, kind", [
        ("Zoom - https://zoom.us/j/1", "virtual"),
        ("Microsoft Teams", "virtual"),
        ("HQ - Boardroom", "in_person"),
        (None, None),
    ])
    def test_kind(self, value, kind):
        assert location_kind(value) == kind

    @pytest.mark.parametrize("a, b, relation", [
        ("Boston Office", "Boston Office", "same"),
        ("HQ - Conference Room B", "Conference Room B", "compatible"),
        ("The Palm - DC", "The Palm Restaurant", "variant"),
        ("HQ - Room 4A", "DC Office - Main Conference Room", "conflict"),
        ("NYC Office - 30th Floor", "Zoom - https://zoom.us/j/9", "format_conflict"),
        (None, "Zoom", "unknown"),
    ])
    def test_compare(self, a, b, relation):
        assert compare_locations(a, b)[1] == relation

    def test_format_normalisation(self):
        assert normalise_format("In-Person") == ("in_person", None)
        assert normalise_format("Virtual") == ("virtual", None)
        assert normalise_format("Internal") == (None, "internal")
        assert normalise_format("???") == (None, None)


class TestText:
    def test_abbreviations_expand(self):
        assert tokens("DD Meeting") >= tokens("Due Diligence")
        assert tokens("QBR") == tokens("Quarterly Business Review")

    def test_similarity_bounds(self):
        assert text_similarity(set(), {"a"}) == 0.0
        assert text_similarity({"a", "b"}, {"a", "b"}) == 1.0
        assert 0 < text_similarity({"a", "b"}, {"a", "c"}) < 1
