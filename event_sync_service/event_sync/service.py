"""Pipeline orchestration: ingest -> dedupe -> match -> reconcile, held in memory."""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .ingest import load_calendar, load_crm
from .logging_config import get_logger
from .matching import Cluster, cluster_duplicates, cross_source_score, match_clusters
from .models import CALENDAR, CRM, SourceRecord
from .reconcile import build_meeting, summarise

log = get_logger(__name__)

NEAR_MISS_FLOOR = 0.30  # unmatched records show candidates scoring at least this much


class ServiceUnavailable(RuntimeError):
    """No successful sync has happened yet, so there is nothing to serve."""


@dataclass
class SyncResult:
    meetings: list[dict[str, Any]]
    records: dict[str, list[SourceRecord]]
    synced_at: str
    stats: dict[str, Any] = field(default_factory=dict)


def _near_misses(cluster: Cluster, others: list[Cluster], crm_side: bool) -> list[dict[str, Any]]:
    out = []
    for other in others:
        best = None
        for a in cluster.members:
            for b in other.members:
                s = cross_source_score(a, b) if crm_side else cross_source_score(b, a)
                if s and (best is None or s.total > best.total):
                    best = s
        if best and best.total >= NEAR_MISS_FLOOR:
            out.append({"source_ids": other.ids, "score": round(best.total, 3), "reasons": best.reasons})
    return sorted(out, key=lambda c: -c["score"])[:3]


def _sort_key(meeting: dict[str, Any]) -> tuple:
    start = meeting["fields"]["start"]["value"]
    return (meeting["date"] or "9999-12-31", start is None, start or "", meeting["id"])


def _safe_build(crm: Cluster | None, cal: Cluster | None, match, failures: list[str]) -> list[dict[str, Any]]:
    """Build one meeting; if that fails, fall back to one meeting per side so no record disappears."""
    ids = (crm.ids if crm else []) + (cal.ids if cal else [])
    try:
        return [build_meeting(crm, cal, match)]
    except Exception:  # noqa: BLE001 - isolate a bad group from the rest of the sync
        log.exception("Failed to reconcile group %s", "+".join(ids))
    if crm and cal:
        log.warning("Falling back to unlinked meetings for %s", "+".join(ids))
        return _safe_build(crm, None, None, failures) + _safe_build(None, cal, None, failures)
    failures.extend(ids)
    return []


def run_sync(crm_path: Path = config.CRM_FILE, calendar_path: Path = config.CALENDAR_FILE) -> SyncResult:
    started = time.perf_counter()
    log.info("Sync started (crm=%s, calendar=%s)", crm_path, calendar_path)

    crm_records = load_crm(crm_path)
    cal_records = load_calendar(calendar_path)

    crm_clusters = cluster_duplicates(crm_records)
    cal_clusters = cluster_duplicates(cal_records)
    matches, crm_only, cal_only = match_clusters(crm_clusters, cal_clusters)

    failures: list[str] = []
    meetings: list[dict[str, Any]] = []
    for m in matches:
        meetings += _safe_build(m.crm, m.calendar, m, failures)
    for c in crm_only:
        for meeting in _safe_build(c, None, None, failures):
            meeting["possible_matches"] = _near_misses(c, cal_clusters, crm_side=True)
            meetings.append(meeting)
    for c in cal_only:
        for meeting in _safe_build(None, c, None, failures):
            meeting["possible_matches"] = _near_misses(c, crm_clusters, crm_side=False)
            meetings.append(meeting)
    meetings.sort(key=_sort_key)

    severity = Counter(c["severity"] for m in meetings for c in m["conflicts"])
    conflict_fields = Counter(c["field"] for m in meetings for c in m["conflicts"] if c["severity"] != "low")
    dq = Counter(i.severity for r in crm_records + cal_records for i in r.issues)
    stats = {
        "source_records": {CRM: len(crm_records), CALENDAR: len(cal_records)},
        "unified_meetings": len(meetings),
        "matched_both": sum(1 for m in meetings if len(m["sources"]) == 2),
        "crm_only": sum(1 for m in meetings if m["sources"] == [CRM]),
        "calendar_only": sum(1 for m in meetings if m["sources"] == [CALENDAR]),
        "duplicates_merged": sum(len(c.members) - 1 for c in crm_clusters + cal_clusters),
        "meetings_with_conflicts": sum(1 for m in meetings if m["conflict_count"]),
        "needs_review": sum(1 for m in meetings if m["needs_review"]),
        "conflicts_by_severity": {k: severity.get(k, 0) for k in ("high", "medium", "low")},
        "conflicts_by_field": dict(conflict_fields.most_common()),
        "data_quality_by_severity": {k: dq.get(k, 0) for k in ("error", "warning", "info")},
        "failed_records": failures,
        "settings": {
            "timezone": config.LOCAL_TZ_NAME,
            "match_threshold": config.MATCH_THRESHOLD,
            "duplicate_threshold": config.DUPLICATE_THRESHOLD,
            "match_weights": config.MATCH_WEIGHTS,
        },
    }
    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats["synced_at"] = synced_at
    stats["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)

    log.info(
        "Sync finished in %.1f ms: %d meetings (%d both, %d CRM-only, %d calendar-only), "
        "%d with conflicts, %d need review",
        stats["duration_ms"], stats["unified_meetings"], stats["matched_both"], stats["crm_only"],
        stats["calendar_only"], stats["meetings_with_conflicts"], stats["needs_review"],
    )
    if failures:
        log.error("Records that could not be reconciled: %s", ", ".join(failures))
    return SyncResult(meetings, {CRM: crm_records, CALENDAR: cal_records}, synced_at, stats)


class SyncService:
    """Thread-safe holder for the latest successful sync result.

    A failed re-sync keeps serving the previous good result and records the error.
    """

    def __init__(self, crm_path: Path = config.CRM_FILE, calendar_path: Path = config.CALENDAR_FILE):
        self.crm_path = crm_path
        self.calendar_path = calendar_path
        self._lock = threading.Lock()
        self._result: SyncResult | None = None
        self.last_error: str | None = None

    def sync(self) -> SyncResult:
        try:
            result = run_sync(self.crm_path, self.calendar_path)  # build outside the lock, swap atomically
        except Exception as exc:
            self.last_error = str(exc)
            if self._result is not None:
                log.error("Sync failed, still serving result from %s: %s", self._result.synced_at, exc)
            else:
                log.error("Sync failed and no previous result is available: %s", exc)
            raise
        with self._lock:
            self._result = result
            self.last_error = None
        return result

    @property
    def has_result(self) -> bool:
        with self._lock:
            return self._result is not None

    @property
    def result(self) -> SyncResult:
        with self._lock:
            current = self._result
        if current is None:
            raise ServiceUnavailable(f"No data synced yet: {self.last_error or 'sync has not run'}")
        return current

    def list_meetings(self, *, source: str | None = None, conflicts_only: bool = False,
                      needs_review: bool = False, q: str | None = None) -> list[dict[str, Any]]:
        items = []
        for meeting in self.result.meetings:
            srcs = meeting["sources"]
            if source == "both" and len(srcs) != 2:
                continue
            if source in (CRM, CALENDAR) and srcs != [source]:
                continue
            if conflicts_only and not meeting["conflict_count"]:
                continue
            if needs_review and not meeting["needs_review"]:
                continue
            summary = summarise(meeting)
            if q:
                haystack = " ".join(str(v) for v in (summary["title"], summary["client"], summary["company"], summary["owner"],
                                                    summary["location"], *summary["source_ids"][CRM], *summary["source_ids"][CALENDAR]))
                if q.lower() not in haystack.lower():
                    continue
            items.append(summary)
        log.debug("list_meetings(source=%s, conflicts=%s, review=%s, q=%r) -> %d", source, conflicts_only, needs_review, q, len(items))
        return items

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        return next((m for m in self.result.meetings if m["id"] == meeting_id), None)

    def source_records(self, source: str) -> list[dict[str, Any]]:
        result = self.result
        return [
            {
                "source_id": r.source_id,
                "raw": r.raw,
                "issues": [i.to_dict(r.source, r.source_id) for i in r.issues],
                "meeting_id": next((m["id"] for m in result.meetings if r.source_id in m["source_ids"][source]), None),
            }
            for r in result.records.get(source, [])
        ]
