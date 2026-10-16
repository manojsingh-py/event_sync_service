"""Ingestion: load each upstream payload and map it into SourceRecord.

Each loader is defensive: a bad field produces a data-quality issue, and a record
that cannot be mapped at all is logged and skipped, so one bad row never stops a sync.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import LOCAL_TZ
from .logging_config import get_logger
from .models import CALENDAR, CRM, DataIssue, SourceRecord
from .normalize import (
    clean_attendees,
    compact,
    display_name,
    domain_stem,
    email_domain,
    is_internal_email,
    location_kind,
    name_from_email,
    name_key,
    normalise_format,
    parse_crm_date,
    parse_crm_time,
    parse_timestamp,
)

log = get_logger(__name__)


class IngestError(RuntimeError):
    """Raised when a whole source cannot be read (missing file, invalid JSON, wrong shape)."""


def load_json_array(path: Path) -> list[dict[str, Any]]:
    log.debug("Reading %s", path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise IngestError(f"Source file not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestError(f"Cannot read {path}: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise IngestError(f"Expected a JSON array in {path}, got {type(payload).__name__}")

    items = [item for item in payload if isinstance(item, dict)]
    if len(items) != len(payload):
        log.warning("%s: ignored %d non-object entries", path.name, len(payload) - len(items))
    return items


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_id(raw: dict[str, Any], key: str, index: int, prefix: str, issues: list[DataIssue]) -> str:
    value = _str_or_none(raw.get(key))
    if value:
        return value
    issues.append(DataIssue(key, "error", "Missing record id; synthetic id assigned"))
    return f"{prefix}-MISSING-{index}"


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------
def crm_to_record(raw: dict[str, Any], index: int) -> SourceRecord:
    issues: list[DataIssue] = []
    source_id = _source_id(raw, "crm_id", index, "CRM", issues)

    meeting_date = parse_crm_date(raw.get("meeting_date"), issues)
    meeting_time = parse_crm_time(raw.get("meeting_time"), issues)
    start = datetime.combine(meeting_date, meeting_time, tzinfo=LOCAL_TZ) if meeting_date and meeting_time else None

    meeting_format, category = normalise_format(raw.get("meeting_type"))
    if raw.get("meeting_type") and meeting_format is None and category is None:
        issues.append(DataIssue("meeting_type", "warning", f"Unknown meeting_type '{raw.get('meeting_type')}'"))

    location = _str_or_none(raw.get("location"))
    if location is None:
        issues.append(DataIssue("location", "info", "No location recorded"))
    # If the CRM gives a concrete location but no explicit format, derive it.
    if meeting_format is None and location and category != "internal":
        meeting_format = location_kind(location)

    client_name = _str_or_none(raw.get("client_name"))
    client_company = _str_or_none(raw.get("client_company"))
    if category is None:
        category = "client" if (client_name or client_company) else "internal"
    if client_name and name_key(client_name) is None:
        issues.append(DataIssue("client_name", "info", f"'{client_name}' is a placeholder, not a person"))

    owner = _str_or_none(raw.get("relationship_owner"))
    people = {k for k in (name_key(client_name), name_key(owner)) if k}
    org_keys = {compact(client_company)} if client_company else set()

    if not _str_or_none(raw.get("subject")):
        issues.append(DataIssue("subject", "warning", "Missing subject"))

    return SourceRecord(
        source=CRM,
        source_id=source_id,
        title=_str_or_none(raw.get("subject")),
        start=start,
        end=None,  # CRM has no duration
        local_date=meeting_date,
        location=location,
        location_kind=location_kind(location),
        meeting_format=meeting_format,
        category=category,
        status=(_str_or_none(raw.get("status")) or "").lower() or None,
        owner_name=owner,
        organizer_email=None,
        client_name=client_name,
        client_company=client_company,
        attendees=[],
        unresolved_attendees=[],
        people=people,
        org_keys=org_keys,
        notes=_str_or_none(raw.get("notes")),
        is_recurring=None,
        created_at=parse_timestamp(raw.get("created_at"), "created_at", issues),
        issues=issues,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def calendar_to_record(raw: dict[str, Any], index: int) -> SourceRecord:
    issues: list[DataIssue] = []
    source_id = _source_id(raw, "event_id", index, "CAL", issues)

    start = parse_timestamp(raw.get("start_time"), "start_time", issues, note_explicit_tz=True)
    end = parse_timestamp(raw.get("end_time"), "end_time", issues, note_explicit_tz=True)
    if start and end and end <= start:
        issues.append(DataIssue("end_time", "warning", "End time is not after start time"))

    attendees, unresolved = clean_attendees(raw.get("attendees"), issues)
    organizer = _str_or_none(raw.get("organizer"))
    organizer = organizer.lower() if organizer else None

    everyone = list(dict.fromkeys(([organizer] if organizer else []) + attendees))
    external = [e for e in everyone if not is_internal_email(e)]
    people = {name_from_email(e) for e in everyone}
    org_keys = {domain_stem(email_domain(e)) for e in external}

    location = _str_or_none(raw.get("location"))
    if location is None:
        issues.append(DataIssue("location", "info", "No location recorded"))
    if _str_or_none(raw.get("description")) is None:
        issues.append(DataIssue("description", "info", "No description"))

    if external or unresolved:
        category = "client"
    elif attendees:
        category = "internal"
    else:
        category = None  # no attendees -> we genuinely don't know

    # For calendar-only meetings the "client" is derived from the external attendees.
    client_name = None
    client_company = None
    if external:
        client_name = ", ".join(display_name(name_from_email(e)) for e in external)
        client_company = ", ".join(sorted({email_domain(e) for e in external}))

    return SourceRecord(
        source=CALENDAR,
        source_id=source_id,
        title=_str_or_none(raw.get("title")),
        start=start,
        end=end,
        local_date=start.date() if start else None,
        location=location,
        location_kind=location_kind(location),
        meeting_format=location_kind(location),
        category=category,
        status=(_str_or_none(raw.get("status")) or "").lower() or None,
        owner_name=None,
        organizer_email=organizer,
        client_name=client_name,
        client_company=client_company,
        attendees=attendees,
        unresolved_attendees=unresolved,
        people=people,
        org_keys=org_keys,
        notes=_str_or_none(raw.get("description")),
        is_recurring=raw.get("is_recurring") if isinstance(raw.get("is_recurring"), bool) else None,
        created_at=parse_timestamp(raw.get("created_at"), "created_at", issues),
        issues=issues,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _load(path: Path, source: str, mapper: Callable[[dict[str, Any], int], SourceRecord]) -> list[SourceRecord]:
    raw_items = load_json_array(path)
    records: list[SourceRecord] = []
    skipped = 0
    for index, raw in enumerate(raw_items):
        try:
            record = mapper(raw, index)
        except Exception:  # noqa: BLE001 - one unmappable row must not stop the sync
            skipped += 1
            log.exception("%s record #%d could not be mapped and was skipped: %r", source, index, raw)
            continue
        for issue in record.issues:
            log.debug("%s %s [%s] %s: %s", source, record.source_id, issue.severity, issue.field, issue.message)
        records.append(record)

    issue_count = sum(1 for r in records for i in r.issues if i.severity != "info")
    log.info("Loaded %d %s records from %s (%d warnings/errors, %d skipped)",
             len(records), source, path.name, issue_count, skipped)
    return records


def load_crm(path: Path) -> list[SourceRecord]:
    return _load(path, CRM, crm_to_record)


def load_calendar(path: Path) -> list[SourceRecord]:
    return _load(path, CALENDAR, calendar_to_record)
