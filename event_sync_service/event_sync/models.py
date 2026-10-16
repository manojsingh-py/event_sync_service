"""Internal data model shared by ingestion, matching and reconciliation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

CRM = "crm"
CALENDAR = "calendar"


@dataclass
class DataIssue:
    """A data-quality observation about one field of one source record."""

    field: str
    severity: str  # "info" | "warning" | "error"
    message: str

    def to_dict(self, source: str, source_id: str) -> dict[str, Any]:
        return {
            "source": source,
            "source_id": source_id,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class SourceRecord:
    """A single upstream record normalised into a common shape.

    Nothing is thrown away: `raw` keeps the original payload so the UI can show it.
    """

    source: str
    source_id: str
    title: str | None
    start: datetime | None          # tz-aware; None when the time is unknown
    end: datetime | None
    local_date: date | None         # date in LOCAL_TZ (known even when time isn't)
    location: str | None
    location_kind: str | None       # "virtual" | "in_person" | None
    meeting_format: str | None      # "virtual" | "in_person" | None (stated or derived)
    category: str | None            # "client" | "internal" | None
    status: str | None              # lower-case source status
    owner_name: str | None          # CRM relationship owner / calendar organizer name
    organizer_email: str | None
    client_name: str | None
    client_company: str | None
    attendees: list[str]            # valid (possibly repaired) emails
    unresolved_attendees: list[str] # entries we could not turn into an email
    people: set[str]                # normalised person names, e.g. "david park"
    org_keys: set[str]              # normalised org identifiers (domain stems / company)
    notes: str | None
    is_recurring: bool | None
    created_at: datetime | None
    issues: list[DataIssue] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"
