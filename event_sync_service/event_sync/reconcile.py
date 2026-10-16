"""Reconciliation: turn a group of source records into one unified meeting.

Each output field records:
  * the resolved value,
  * which source/record it came from (provenance),
  * the rule that picked it,
  * every candidate value from every source,
  * a status: agreed | single_source | complementary | variant | conflict | missing.

Conflicts are never silently resolved: a value is picked so the list is usable,
but the conflict is surfaced with a severity so a human can decide.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .config import LOW_CONFIDENCE_THRESHOLD
from .logging_config import get_logger
from .matching import Cluster, Match
from .models import CALENDAR, CRM, SourceRecord
from .normalize import compare_locations, display_name, is_internal_email, name_from_email, name_key, normalise_location

STATUS_GROUP = {
    "confirmed": "active",
    "scheduled": "active",
    "completed": "active",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "tentative": "tentative",
}
STATUS_RANK = {"cancelled": 3, "tentative": 2, "active": 1, "unknown": 0}
LOCATION_RELATION_RANK = {"same": 0, "compatible": 1, "variant": 2, "conflict": 3, "format_conflict": 4}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
SOURCE_LABEL = {CRM: "CRM", CALENDAR: "Calendar"}

log = get_logger(__name__)


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z") if dt else "no time"


def _cand(rec: SourceRecord, value: Any, note: str | None = None) -> dict[str, Any]:
    out = {"source": rec.source, "source_id": rec.source_id, "value": value}
    if note:
        out["note"] = note
    return out


class MeetingBuilder:
    def __init__(self, crm: Cluster | None, cal: Cluster | None, match: Match | None):
        self.crm_cluster = crm
        self.cal_cluster = cal
        self.match = match
        self.crm = crm.primary if crm else None
        self.cal = cal.primary if cal else None
        # Primary first so "first record with a value" == "most current record".
        self.crm_records = self._ordered(crm)
        self.cal_records = self._ordered(cal)
        self.records = self.crm_records + self.cal_records
        self.fields: dict[str, dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []

    @staticmethod
    def _ordered(cluster: Cluster | None) -> list[SourceRecord]:
        if not cluster:
            return []
        return [cluster.primary] + [m for m in cluster.members if m is not cluster.primary]

    # -- helpers -------------------------------------------------------------
    def _set(self, name: str, label: str, value: Any, rec: SourceRecord | None, rule: str, candidates: list[dict], status: str) -> None:
        self.fields[name] = {
            "label": label,
            "value": value,
            "source": rec.source if rec else None,
            "source_id": rec.source_id if rec else None,
            "rule": rule,
            "status": status,
            "candidates": candidates,
        }

    def _conflict(self, field: str, severity: str, message: str) -> None:
        self.conflicts.append({"field": field, "severity": severity, "message": message, "candidates": self.fields.get(field, {}).get("candidates", [])})

    def _basic_status(self, candidates: list[dict], normaliser=lambda v: v) -> str:
        if not candidates:
            return "missing"
        if len({c["source"] for c in candidates}) == 1 and len(candidates) == 1:
            return "single_source"
        values = {normaliser(c["value"]) for c in candidates}
        return "agreed" if len(values) == 1 else "variant"

    # -- fields --------------------------------------------------------------
    def title(self) -> None:
        cands = [_cand(r, r.title) for r in self.records if r.title]
        rec = next((r for r in self.cal_records if r.title), None) or next((r for r in self.crm_records if r.title), None)
        status = self._basic_status(cands, lambda v: (v or "").strip().lower())
        self._set("title", "Title", rec.title if rec else "(untitled)", rec,
                  "Calendar title preferred (it names the client); CRM subject as fallback", cands, status)

    def start(self) -> None:
        cands = [
            _cand(r, r.start.isoformat() if r.start else None, None if r.start else f"date only: {r.local_date.isoformat() if r.local_date else 'unknown'}")
            for r in self.records
        ]
        timed = [r for r in self.records if r.start]
        rec = next((r for r in self.cal_records if r.start), None) or next((r for r in self.crm_records if r.start), None)
        rule = "Calendar is the system of record for scheduling; the most recently created invite wins"
        if not timed:
            self._set("start", "Start", None, None, rule, cands, "missing")
            return
        instants = {r.start.timestamp() for r in timed}
        if len(instants) > 1:
            spread = (max(instants) - min(instants)) / 60
            self._set("start", "Start", rec.start.isoformat(), rec, rule, cands, "conflict")
            detail = "; ".join(f"{r.source_id} {_fmt_time(r.start)}" for r in timed)
            self._conflict("start", "high" if spread >= 60 else "medium", f"Start times disagree by {int(spread)} min: {detail}")
            return
        if len(timed) < len(self.records):
            status = "complementary" if len(self.records) > 1 else "single_source"
        else:
            status = "single_source" if len(self.records) == 1 else "agreed"
        self._set("start", "Start", rec.start.isoformat(), rec, rule, cands, status)

    def end(self) -> None:
        cands = [_cand(r, r.end.isoformat()) for r in self.cal_records if r.end]
        rec = next((r for r in self.cal_records if r.end), None)
        status = "missing" if not cands else ("single_source" if len(cands) == 1 else self._basic_status(cands))
        if status == "variant":
            status = "conflict"
        self._set("end", "End", rec.end.isoformat() if rec else None, rec,
                  "Only the calendar records a duration", cands, status)
        if status == "conflict":
            self._conflict("end", "low", "Duplicate calendar invites have different end times")

    def status(self) -> None:
        cands = [_cand(r, r.status, STATUS_GROUP.get(r.status or "", "unknown")) for r in self.records if r.status]
        if not cands:
            self._set("status", "Status", None, None, "", cands, "missing")
            return
        groups = {STATUS_GROUP.get(c["value"], "unknown") for c in cands}
        winner_group = max(groups, key=lambda g: STATUS_RANK[g])
        # Prefer the CRM wording within the winning group (it is more specific, e.g. "completed").
        rec = next(r for r in self.crm_records + self.cal_records if STATUS_GROUP.get(r.status or "", "unknown") == winner_group and r.status)
        rule = "A cancellation in any source wins (stale invites are common); otherwise CRM wording is used"
        if len(groups) > 1:
            self._set("status", "Status", rec.status, rec, rule, cands, "conflict")
            detail = "; ".join(f"{c['source_id']} '{c['value']}'" for c in cands)
            self._conflict("status", "high" if "cancelled" in groups else "medium", f"Status disagrees between records: {detail}")
        else:
            self._set("status", "Status", rec.status, rec, rule, cands, self._basic_status(cands, lambda v: STATUS_GROUP.get(v, v)))

    def meeting_format(self) -> None:
        cands = [_cand(r, r.meeting_format, "stated in CRM" if r.source == CRM and r.raw.get("meeting_type") else "derived from location")
                 for r in self.records if r.meeting_format]
        rec = next((r for r in self.cal_records if r.meeting_format), None) or next((r for r in self.crm_records if r.meeting_format), None)
        rule = "Derived from the calendar location (what attendees actually join); CRM meeting_type as fallback"
        if not cands:
            self._set("format", "Format", None, None, rule, cands, "missing")
            return
        values = {c["value"] for c in cands}
        if len(values) > 1:
            self._set("format", "Format", rec.meeting_format, rec, rule, cands, "conflict")
            detail = "; ".join(f"{c['source_id']} {c['value'].replace('_', '-')}" for c in cands)
            self._conflict("format", "high", f"Virtual vs in-person disagreement: {detail}")
        else:
            self._set("format", "Format", rec.meeting_format, rec, rule, cands, self._basic_status(cands))

    def location(self) -> None:
        located = [r for r in self.records if r.location]
        cands = [_cand(r, r.location) for r in located]
        if not located:
            self._set("location", "Location", None, None, "", cands, "missing")
            return
        worst = "same"
        for i in range(len(located)):
            for j in range(i + 1, len(located)):
                _, rel = compare_locations(located[i].location, located[j].location)
                if LOCATION_RELATION_RANK[rel] > LOCATION_RELATION_RANK[worst]:
                    worst = rel
        if len(located) == 1:
            rec = located[0]
            status = "single_source" if len(self.records) == 1 else "complementary"
            self._set("location", "Location", rec.location, rec, "Only one record has a location", cands, status)
            return
        if worst in ("same", "compatible"):
            # Values describe the same place; keep the most specific wording.
            rec = max(located, key=lambda r: len(normalise_location(r.location)))
            status = "agreed" if worst == "same" else "complementary"
            self._set("location", "Location", rec.location, rec,
                      "Values describe the same place; the most specific one is kept", cands, status)
            return
        rec = next((r for r in self.cal_records if r.location), located[0])
        rule = "Calendar location preferred (it is what attendees received)"
        detail = "; ".join(f"{r.source_id} '{r.location}'" for r in located)
        if worst == "variant":
            self._set("location", "Location", rec.location, rec, rule, cands, "variant")
            self._conflict("location", "low", f"Location wording differs but likely the same place: {detail}")
        else:
            self._set("location", "Location", rec.location, rec, rule, cands, "conflict")
            severity = "high" if worst == "format_conflict" else "medium"
            self._conflict("location", severity, f"Locations disagree: {detail}")

    def category(self) -> None:
        cands = [_cand(r, r.category) for r in self.records if r.category]
        values = {c["value"] for c in cands}
        value = "client" if "client" in values else (next(iter(values)) if values else None)
        rec = next((r for r in self.records if r.category == value), None)
        status = self._basic_status(cands)
        if status == "variant":
            status = "conflict"
        self._set("category", "Category", value, rec, "Client if any source shows a client/external attendee", cands, status)
        if status == "conflict":
            self._conflict("category", "medium", "One source treats this as internal, the other shows client participants")

    def client(self) -> None:
        cands = [_cand(r, r.client_name, "derived from attendee emails" if r.source == CALENDAR else None) for r in self.records if r.client_name]
        crm_rec = next((r for r in self.crm_records if name_key(r.client_name)), None)
        cal_rec = next((r for r in self.cal_records if r.client_name), None)
        rec = crm_rec or cal_rec
        rule = "CRM client is authoritative; calendar external attendees as fallback (or when CRM has a placeholder)"
        if not rec:
            placeholder = next((r for r in self.crm_records if r.client_name), None)
            self._set("client", "Client", placeholder.client_name if placeholder else None, placeholder, rule, cands,
                      "single_source" if placeholder else "missing")
            return
        status = "single_source" if len(cands) == 1 else "complementary"
        self._set("client", "Client", rec.client_name, rec, rule, cands, status)
        # Sanity check: the CRM client should be on the invite if the invite has external people.
        if crm_rec and self.cal_records:
            key = name_key(crm_rec.client_name)
            invitees = set().union(*(r.people for r in self.cal_records))
            has_external = any(r.org_keys for r in self.cal_records)
            if has_external and key not in invitees:
                self._conflict("client", "low", f"CRM client '{crm_rec.client_name}' is not on the calendar invite")

    def company(self) -> None:
        cands = [_cand(r, r.client_company, "attendee email domains" if r.source == CALENDAR else None) for r in self.records if r.client_company]
        rec = next((r for r in self.crm_records if r.client_company), None) or next((r for r in self.cal_records if r.client_company), None)
        status = "missing" if not cands else ("single_source" if len(cands) == 1 else "complementary")
        self._set("company", "Company", rec.client_company if rec else None, rec,
                  "CRM company name preferred; calendar email domains as fallback", cands, status)

    def owner(self) -> None:
        cands = [_cand(r, r.owner_name, "CRM relationship owner") for r in self.crm_records if r.owner_name]
        cands += [_cand(r, display_name(name_from_email(r.organizer_email)), "calendar organizer") for r in self.cal_records if r.organizer_email]
        rec = next((r for r in self.crm_records if r.owner_name), None)
        if rec:
            value = rec.owner_name
        else:
            rec = next((r for r in self.cal_records if r.organizer_email), None)
            value = display_name(name_from_email(rec.organizer_email)) if rec else None
        keys = {name_key(c["value"]) for c in cands}
        status = "missing" if not cands else ("single_source" if len(cands) == 1 else ("agreed" if len(keys) == 1 else "variant"))
        self._set("owner", "Owner", value, rec, "CRM relationship owner preferred; calendar organizer as fallback", cands, status)
        if status == "variant" and self.crm_records and self.cal_records:
            self._conflict("owner", "low", "CRM relationship owner differs from the calendar organizer: "
                           + "; ".join(f"{c['source_id']} {c['value']} ({c['note']})" for c in cands))

    def attendees(self) -> None:
        people: dict[str, dict[str, Any]] = {}
        for r in self.cal_records:
            for email in ([r.organizer_email] if r.organizer_email else []) + r.attendees:
                entry = people.setdefault(email, {"email": email, "name": display_name(name_from_email(email)),
                                                  "internal": is_internal_email(email), "sources": []})
                if r.source_id not in entry["sources"]:
                    entry["sources"].append(r.source_id)
            for raw in r.unresolved_attendees:
                entry = people.setdefault(f"unresolved:{raw}", {"email": None, "name": raw, "internal": None, "unresolved": True, "sources": []})
                entry["sources"].append(r.source_id)
        for r in self.crm_records:
            for label, value in (("client", r.client_name), ("owner", r.owner_name)):
                key = name_key(value)
                if not key:
                    continue
                existing = next((p for p in people.values() if name_key(p["name"]) == key), None)
                if existing:
                    existing["sources"].append(r.source_id)
                else:
                    people[f"crm:{key}"] = {"email": None, "name": value, "internal": label == "owner", "sources": [r.source_id]}
        value = list(people.values())
        cands = [_cand(r, len(r.attendees) + (1 if r.organizer_email and r.organizer_email not in r.attendees else 0), "invitees")
                 for r in self.cal_records]
        status = "missing" if not value else ("single_source" if len(self.records) == 1 else "complementary")
        self._set("attendees", "Attendees", value, self.cal or self.crm,
                  "Union of everyone named in any source, with per-person provenance", cands, status)

    def text_fields(self) -> None:
        notes = [_cand(r, r.notes) for r in self.crm_records if r.notes]
        self._set("crm_notes", "CRM notes", notes[0]["value"] if notes else None, self.crm if notes else None,
                  "Shown as-is (free text, not reconciled)", notes, "single_source" if notes else "missing")
        desc = [_cand(r, r.notes) for r in self.cal_records if r.notes]
        self._set("calendar_description", "Calendar description", desc[0]["value"] if desc else None,
                  self.cal if desc else None, "Shown as-is (free text, not reconciled)", desc,
                  "missing" if not desc else ("single_source" if len(desc) == 1 else "variant"))
        rec = next((r for r in self.cal_records if r.is_recurring is not None), None)
        cands = [_cand(r, r.is_recurring) for r in self.cal_records if r.is_recurring is not None]
        self._set("recurring", "Recurring", rec.is_recurring if rec else None, rec,
                  "Only the calendar knows about recurrence", cands, "single_source" if rec else "missing")

    # -- assembly ------------------------------------------------------------
    def build(self) -> dict[str, Any]:
        self.title()
        self.start()
        self.end()
        self.status()
        self.meeting_format()
        self.location()
        self.category()
        self.client()
        self.company()
        self.owner()
        self.attendees()
        self.text_fields()

        ids = sorted(r.key for r in self.records)
        meeting_id = "MTG-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]

        local_date = next((r.local_date for r in self.cal_records + self.crm_records if r.local_date), None)
        start_value = self.fields["start"]["value"]
        if start_value:
            local_date = datetime.fromisoformat(start_value).date()

        duplicates = []
        for cluster in (self.crm_cluster, self.cal_cluster):
            if cluster and len(cluster.members) > 1:
                duplicates.append({
                    "source": cluster.primary.source,
                    "kept": cluster.primary.source_id,
                    "merged": [m.source_id for m in cluster.members if m is not cluster.primary],
                    "rule": "Same-day records with overlapping attendees/org/title are one meeting; the most recently created record is treated as current",
                    "links": [{"a": a, "b": b, **s.to_dict()} for a, b, s in cluster.links],
                })

        data_quality = [i.to_dict(r.source, r.source_id) for r in self.records for i in r.issues]

        match_info = None
        if self.match:
            match_info = {
                **self.match.score.to_dict(),
                "pair": {"crm": self.match.best_pair[0], "calendar": self.match.best_pair[1]},
                "confidence": "high" if self.match.score.total >= LOW_CONFIDENCE_THRESHOLD else "low",
            }

        self.conflicts.sort(key=lambda c: -SEVERITY_RANK[c["severity"]])
        for c in self.conflicts:
            log.debug("%s [%s] %s conflict: %s", "+".join(ids), c["severity"], c["field"], c["message"])
        review_reasons = [f"{c['severity']} conflict on {c['field']}" for c in self.conflicts if c["severity"] in ("high", "medium")]
        if match_info and match_info["confidence"] == "low":
            review_reasons.append(f"low-confidence match ({match_info['score']:.2f})")
        if duplicates:
            review_reasons.append("duplicate records were merged")
        if any(i["severity"] == "error" for i in data_quality):
            review_reasons.append("source data errors")

        sources = [s for s in (CRM, CALENDAR) if (self.crm_records if s == CRM else self.cal_records)]
        return {
            "id": meeting_id,
            "date": local_date.isoformat() if local_date else None,
            "time_known": start_value is not None,
            "sources": sources,
            "source_ids": {CRM: [r.source_id for r in self.crm_records], CALENDAR: [r.source_id for r in self.cal_records]},
            "fields": self.fields,
            "conflicts": self.conflicts,
            "conflict_count": sum(1 for c in self.conflicts if c["severity"] in ("high", "medium")),
            "max_severity": self.conflicts[0]["severity"] if self.conflicts else None,
            "match": match_info,
            "duplicates": duplicates,
            "data_quality": data_quality,
            "needs_review": bool(review_reasons),
            "review_reasons": review_reasons,
            "possible_matches": [],
            "records": [{"source": r.source, "source_id": r.source_id, "raw": r.raw} for r in self.records],
        }


def build_meeting(crm: Cluster | None, cal: Cluster | None, match: Match | None) -> dict[str, Any]:
    return MeetingBuilder(crm, cal, match).build()


def summarise(meeting: dict[str, Any]) -> dict[str, Any]:
    """Compact view for list endpoints."""
    f = meeting["fields"]
    return {
        "id": meeting["id"],
        "date": meeting["date"],
        "time_known": meeting["time_known"],
        "start": f["start"]["value"],
        "end": f["end"]["value"],
        "title": f["title"]["value"],
        "client": f["client"]["value"],
        "company": f["company"]["value"],
        "owner": f["owner"]["value"],
        "location": f["location"]["value"],
        "format": f["format"]["value"],
        "category": f["category"]["value"],
        "status": f["status"]["value"],
        "sources": meeting["sources"],
        "source_ids": meeting["source_ids"],
        "conflict_count": meeting["conflict_count"],
        "max_severity": meeting["max_severity"],
        "conflict_fields": sorted({c["field"] for c in meeting["conflicts"]}),
        "needs_review": meeting["needs_review"],
        "match_score": meeting["match"]["score"] if meeting["match"] else None,
        "has_duplicates": bool(meeting["duplicates"]),
        "data_quality_count": len([i for i in meeting["data_quality"] if i["severity"] != "info"]),
    }
