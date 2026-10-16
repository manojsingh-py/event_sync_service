"""Duplicate detection and cross-source matching."""
from __future__ import annotations

from event_sync import config
from event_sync.ingest import calendar_to_record, crm_to_record, load_calendar, load_crm
from event_sync.matching import cluster_duplicates, cross_source_score, duplicate_score, match_clusters

from .test_ingest import CAL_OK, CRM_OK


def by_id(records):
    return {r.source_id: r for r in records}


CAL = by_id(load_calendar(config.CALENDAR_FILE))
CRM_R = by_id(load_crm(config.CRM_FILE))


class TestDuplicates:
    def test_pinnacle_invites_are_duplicates(self):
        s = duplicate_score(CAL["CAL-A5"], CAL["CAL-A6"])
        assert s.total >= config.DUPLICATE_THRESHOLD

    def test_weekly_series_is_not_a_duplicate(self):
        assert duplicate_score(CAL["CAL-A3"], CAL["CAL-A18"]) is None  # different days

    def test_same_day_unrelated_events_are_not_duplicates(self):
        assert duplicate_score(CAL["CAL-A19"], CAL["CAL-A20"]).total < config.DUPLICATE_THRESHOLD

    def test_cross_source_pair_is_never_a_duplicate(self):
        assert duplicate_score(CRM_R["CRM-1001"], CAL["CAL-A1"]) is None

    def test_clustering_keeps_latest_as_primary(self):
        clusters = cluster_duplicates(list(CAL.values()))
        multi = [c for c in clusters if len(c.members) > 1]
        assert len(multi) == 1
        assert multi[0].primary.source_id == "CAL-A6"  # created later than A5
        assert sorted(multi[0].ids) == ["CAL-A5", "CAL-A6"]

    def test_no_crm_duplicates(self):
        assert all(len(c.members) == 1 for c in cluster_duplicates(list(CRM_R.values())))


class TestCrossSource:
    def test_different_day_is_impossible(self):
        assert cross_source_score(CRM_R["CRM-1001"], CAL["CAL-A8"]) is None

    def test_strong_match_explains_itself(self):
        s = cross_source_score(CRM_R["CRM-1001"], CAL["CAL-A1"])
        assert s.total > 0.9
        assert set(s.components) == set(config.MATCH_WEIGHTS)
        assert any("client 'David Park'" in r for r in s.reasons)

    def test_time_conflict_still_matches(self):
        # CRM-1016 says 13:00, calendar 15:00; people/title/location still agree.
        assert cross_source_score(CRM_R["CRM-1016"], CAL["CAL-A17"]).total >= config.MATCH_THRESHOLD

    def test_missing_crm_time_is_neutral(self):
        s = cross_source_score(CRM_R["CRM-1007"], CAL["CAL-A8"])
        assert s.components["time"] == 0.5 and s.total >= config.MATCH_THRESHOLD

    def test_same_day_wrong_meeting_stays_below_threshold(self):
        assert cross_source_score(CRM_R["CRM-1003"], CAL["CAL-A7"]).total < config.MATCH_THRESHOLD
        assert cross_source_score(CRM_R["CRM-1017"], CAL["CAL-A19"]).total < config.MATCH_THRESHOLD

    def test_scores_are_bounded(self):
        for crm in CRM_R.values():
            for cal in CAL.values():
                s = cross_source_score(crm, cal)
                if s:
                    assert 0.0 <= s.total <= 1.0


class TestAssignment:
    def test_one_to_one(self):
        crm_a = crm_to_record(CRM_OK, 0)
        crm_b = crm_to_record({**CRM_OK, "crm_id": "CRM-2"}, 1)  # identical twin
        cal = calendar_to_record(CAL_OK, 0)
        matches, crm_only, cal_only = match_clusters(
            [c for c in cluster_duplicates([crm_a])] + [c for c in cluster_duplicates([crm_b])],
            cluster_duplicates([cal]),
        )
        assert len(matches) == 1 and len(crm_only) == 1 and cal_only == []

    def test_no_candidates(self):
        matches, crm_only, cal_only = match_clusters([], cluster_duplicates([calendar_to_record(CAL_OK, 0)]))
        assert matches == [] and crm_only == [] and len(cal_only) == 1
