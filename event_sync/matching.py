"""Record linkage.

Two problems, same toolkit:

1. Duplicate detection *within* a source (e.g. two calendar invites for one meeting).
2. Matching *across* sources (CRM record <-> calendar event).

Both use an explainable weighted-evidence score in [0, 1] (a hand-tuned linear
model in the spirit of Fellegi-Sunter record linkage). Every score keeps its
per-signal components and human-readable reasons so the UI can show *why*.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import (
    DUPLICATE_THRESHOLD,
    DUPLICATE_TIME_WINDOW_MIN,
    DUPLICATE_WEIGHTS,
    MATCH_THRESHOLD,
    MATCH_TIME_WINDOW_MIN,
    MATCH_WEIGHTS,
)
from .logging_config import get_logger
from .models import SourceRecord
from .normalize import company_matches_domain, compare_locations, name_key, text_similarity, tokens

log = get_logger(__name__)


@dataclass
class Score:
    total: float
    components: dict[str, float]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"score": round(self.total, 3), "components": {k: round(v, 3) for k, v in self.components.items()}, "reasons": self.reasons}


def _minutes_apart(a: SourceRecord, b: SourceRecord) -> float | None:
    if a.start is None or b.start is None:
        return None
    return abs((a.start - b.start).total_seconds()) / 60


def _time_score(a: SourceRecord, b: SourceRecord, window: int, reasons: list[str]) -> float:
    minutes = _minutes_apart(a, b)
    if minutes is None:
        reasons.append("time: one side has no start time (neutral)")
        return 0.5
    score = max(0.0, 1 - minutes / window)
    reasons.append("time: identical start" if minutes == 0 else f"time: starts {int(minutes)} min apart")
    return score


def _weighted(components: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[k] * components[k] for k in weights)


# ---------------------------------------------------------------------------
# Cross-source: CRM <-> Calendar
# ---------------------------------------------------------------------------
def _people_score(crm: SourceRecord, cal: SourceRecord, reasons: list[str]) -> float:
    if crm.category == "internal":
        if cal.category == "internal":
            reasons.append("people: both are internal meetings")
            return 0.6
        if cal.category is None:
            reasons.append("people: CRM internal, calendar has no attendees")
            return 0.3
        reasons.append("people: CRM internal but calendar has external attendees")
        return 0.0

    client = name_key(crm.client_name)
    if client and client in cal.people:
        reasons.append(f"people: client '{crm.client_name}' is a calendar attendee")
        return 1.0
    if crm.client_company and any(company_matches_domain(crm.client_company, d) for d in cal.org_keys):
        reasons.append(f"people: attendee email domain matches company '{crm.client_company}'")
        return 0.8
    if crm.client_company and tokens(crm.client_company) & tokens(cal.title):
        reasons.append(f"people: company '{crm.client_company}' named in calendar title")
        return 0.6
    reasons.append("people: no client overlap")
    return 0.0


def _location_score(crm: SourceRecord, cal: SourceRecord, reasons: list[str]) -> float:
    if crm.location and cal.location:
        score, relation = compare_locations(crm.location, cal.location)
        reasons.append(f"location: {relation.replace('_', ' ')} ('{crm.location}' vs '{cal.location}')")
        return score
    # Fall back to comparing the meeting format (virtual / in person).
    if crm.meeting_format and cal.meeting_format:
        same = crm.meeting_format == cal.meeting_format
        reasons.append(f"location: format {'agrees' if same else 'differs'} ({crm.meeting_format} vs {cal.meeting_format})")
        return 0.7 if same else 0.2
    reasons.append("location: not enough data (neutral)")
    return 0.5


def _owner_score(crm: SourceRecord, cal: SourceRecord, reasons: list[str]) -> float:
    owner = name_key(crm.owner_name)
    if not owner:
        return 0.5
    if owner in cal.people:
        reasons.append(f"owner: '{crm.owner_name}' is organizer/attendee")
        return 1.0
    reasons.append(f"owner: '{crm.owner_name}' not on the invite")
    return 0.0


def cross_source_score(crm: SourceRecord, cal: SourceRecord) -> Score | None:
    """Score a CRM/calendar pair. Returns None when the pair is impossible (different day)."""
    if crm.local_date is None or cal.local_date is None or crm.local_date != cal.local_date:
        return None
    reasons: list[str] = [f"date: same day ({crm.local_date.isoformat()})"]
    title_sim = text_similarity(tokens(crm.title, crm.client_company), tokens(cal.title))
    components = {
        "time": _time_score(crm, cal, MATCH_TIME_WINDOW_MIN, reasons),
        "people": _people_score(crm, cal, reasons),
        "title": title_sim,
        "location": _location_score(crm, cal, reasons),
        "owner": _owner_score(crm, cal, reasons),
    }
    reasons.append(f"title: similarity {title_sim:.2f} ('{crm.title}' vs '{cal.title}')")
    return Score(_weighted(components, MATCH_WEIGHTS), components, reasons)


# ---------------------------------------------------------------------------
# Same-source duplicates
# ---------------------------------------------------------------------------
def duplicate_score(a: SourceRecord, b: SourceRecord) -> Score | None:
    if a.source != b.source or a.local_date is None or a.local_date != b.local_date:
        return None
    reasons: list[str] = [f"date: same day ({a.local_date.isoformat()})"]
    union = a.people | b.people
    jaccard = len(a.people & b.people) / len(union) if union else 0.0
    shared_orgs = a.org_keys & b.org_keys
    components = {
        "time": _time_score(a, b, DUPLICATE_TIME_WINDOW_MIN, reasons),
        "people": jaccard,
        "title": text_similarity(tokens(a.title), tokens(b.title)),
        "org": 1.0 if shared_orgs else 0.0,
    }
    reasons.append(f"people: attendee overlap {jaccard:.2f}")
    if shared_orgs:
        reasons.append(f"org: same external organisation ({', '.join(sorted(shared_orgs))})")
    reasons.append(f"title: similarity {components['title']:.2f}")
    return Score(_weighted(components, DUPLICATE_WEIGHTS), components, reasons)


@dataclass
class Cluster:
    """One or more records from the same source that describe the same meeting."""

    primary: SourceRecord
    members: list[SourceRecord]
    links: list[tuple[str, str, Score]] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [m.source_id for m in self.members]


def cluster_duplicates(records: list[SourceRecord]) -> list[Cluster]:
    """Union-find over pairs scoring above DUPLICATE_THRESHOLD.

    Recurring-series instances on different days never collide because the same
    local date is a hard requirement.
    """
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    links: list[tuple[int, int, Score]] = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            score = duplicate_score(records[i], records[j])
            if score is None:
                continue
            log.debug("duplicate check %s vs %s: %.3f %s", records[i].source_id, records[j].source_id,
                      score.total, score.to_dict()["components"])
            if score.total >= DUPLICATE_THRESHOLD:
                log.info("Duplicate detected: %s ~ %s (score %.3f)", records[i].source_id, records[j].source_id, score.total)
                links.append((i, j, score))
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = {}
    for i in range(len(records)):
        groups.setdefault(find(i), []).append(i)

    clusters: list[Cluster] = []
    for root, idxs in groups.items():
        members = [records[i] for i in idxs]
        # The most recently created record is treated as the current version.
        primary = max(members, key=lambda r: (r.created_at.timestamp() if r.created_at else float("-inf"), r.source_id))
        cluster_links = [(records[i].source_id, records[j].source_id, s) for i, j, s in links if find(i) == root]
        clusters.append(Cluster(primary=primary, members=members, links=cluster_links))
    return clusters


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
@dataclass
class Match:
    crm: Cluster
    calendar: Cluster
    score: Score
    best_pair: tuple[str, str]


def match_clusters(crm_clusters: list[Cluster], cal_clusters: list[Cluster]) -> tuple[list[Match], list[Cluster], list[Cluster]]:
    """Greedy one-to-one assignment by descending score.

    With a hard same-day gate the candidate graph is tiny, so greedy gives the
    same result as an optimal (Hungarian) assignment here while staying easy to explain.
    """
    candidates: list[tuple[float, int, int, Score, tuple[str, str]]] = []
    for ci, crm_c in enumerate(crm_clusters):
        for ki, cal_c in enumerate(cal_clusters):
            best: tuple[Score, tuple[str, str]] | None = None
            for crm_r in crm_c.members:
                for cal_r in cal_c.members:
                    s = cross_source_score(crm_r, cal_r)
                    if s is None:
                        continue
                    log.debug("match candidate %s vs %s: %.3f %s", crm_r.source_id, cal_r.source_id,
                              s.total, s.to_dict()["components"])
                    if best is None or s.total > best[0].total:
                        best = (s, (crm_r.source_id, cal_r.source_id))
            if best and best[0].total >= MATCH_THRESHOLD:
                candidates.append((best[0].total, ci, ki, best[0], best[1]))

    candidates.sort(key=lambda c: (-c[0], c[4]))
    used_crm: set[int] = set()
    used_cal: set[int] = set()
    matches: list[Match] = []
    for _, ci, ki, score, pair in candidates:
        if ci in used_crm or ki in used_cal:
            continue
        used_crm.add(ci)
        used_cal.add(ki)
        matches.append(Match(crm_clusters[ci], cal_clusters[ki], score, pair))
        log.debug("Matched %s <-> %s (score %.3f)", "+".join(crm_clusters[ci].ids), "+".join(cal_clusters[ki].ids), score.total)

    crm_only = [c for i, c in enumerate(crm_clusters) if i not in used_crm]
    cal_only = [c for i, c in enumerate(cal_clusters) if i not in used_cal]
    log.info("Matching: %d pairs above threshold %.2f, %d CRM-only, %d calendar-only",
             len(matches), MATCH_THRESHOLD, len(crm_only), len(cal_only))
    return matches, crm_only, cal_only


__all__ = ["Cluster", "Match", "Score", "cluster_duplicates", "cross_source_score", "duplicate_score", "match_clusters", "MATCH_THRESHOLD"]
