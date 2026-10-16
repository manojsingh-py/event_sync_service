"""Low-level parsing / normalisation helpers. Pure functions, no I/O."""
from __future__ import annotations

import re
from datetime import date, datetime, time

from .config import INTERNAL_DOMAINS, LOCAL_TZ
from .models import DataIssue

EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")
STRICT_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
URL_RE = re.compile(r"https?://\S+")

# Lenient formats accepted for CRM dates, in priority order. The first one is the
# expected format; anything else is accepted but reported as a data-quality warning.
CRM_DATE_FORMATS = ["%Y-%m-%d", "%m-%d/%Y", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"]
CRM_TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"]

VIRTUAL_KEYWORDS = ("zoom", "teams", "virtual", "webex", "google meet", "meet.google", "http", "dial-in")

STOPWORDS = {
    "the", "a", "an", "and", "of", "with", "for", "re", "to", "on", "in", "at",
    "meeting", "call", "session", "mtg",
}
# Domain abbreviations seen in titles. Expanding them lets "DD Meeting" match
# "Due Diligence Review" without any learned model.
ABBREVIATIONS = {
    "dd": ["due", "diligence"],
    "lpac": ["lp", "advisory", "committee"],
    "qbr": ["quarterly", "business", "review"],
    "infra": ["infrastructure"],
    "followup": ["follow", "up"],
}
# Values that look like a client name but carry no identity.
PLACEHOLDER_NAMES = {"multiple", "various", "tbd", "n/a", "na", "none", "unknown"}


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def parse_timestamp(value: object, field: str, issues: list[DataIssue], *, note_explicit_tz: bool = False) -> datetime | None:
    """Parse an ISO-ish timestamp into a tz-aware datetime.

    Naive timestamps are interpreted in LOCAL_TZ. Non-standard but parseable
    values are accepted with a warning so nothing is silently dropped.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        issues.append(DataIssue(field, "warning", "Missing value"))
        return None
    if not isinstance(value, str):
        issues.append(DataIssue(field, "error", f"Unexpected type {type(value).__name__}"))
        return None

    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        issues.append(DataIssue(field, "error", f"Unparseable timestamp '{text}'"))
        return None

    if not STRICT_TS_RE.match(text):
        issues.append(DataIssue(field, "warning", f"Non-standard timestamp format '{text}' (parsed leniently)"))

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ)

    local = parsed.astimezone(LOCAL_TZ)
    if note_explicit_tz:
        issues.append(
            DataIssue(
                field,
                "info",
                f"Timestamp carries an explicit offset ('{text}') while other records are naive; "
                f"converted to {local.strftime('%H:%M %Z')} local time",
            )
        )
    return local


def parse_crm_date(value: object, issues: list[DataIssue]) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        issues.append(DataIssue("meeting_date", "error", "Missing meeting date"))
        return None
    text = str(value).strip()
    for idx, fmt in enumerate(CRM_DATE_FORMATS):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if idx > 0:
            issues.append(
                DataIssue("meeting_date", "warning", f"Malformed date '{text}' interpreted as {parsed.isoformat()} (format {fmt})")
            )
        return parsed
    issues.append(DataIssue("meeting_date", "error", f"Unparseable date '{text}'"))
    return None


def parse_crm_time(value: object, issues: list[DataIssue]) -> time | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        issues.append(DataIssue("meeting_time", "warning", "Missing meeting time; record is date-only"))
        return None
    text = str(value).strip()
    for idx, fmt in enumerate(CRM_TIME_FORMATS):
        try:
            parsed = datetime.strptime(text.upper() if "%p" in fmt else text, fmt).time()
        except ValueError:
            continue
        if idx > 0:
            issues.append(DataIssue("meeting_time", "warning", f"Non-standard time '{text}' interpreted as {parsed.strftime('%H:%M')}"))
        return parsed
    issues.append(DataIssue("meeting_time", "error", f"Unparseable time '{text}'"))
    return None


# ---------------------------------------------------------------------------
# People / organisations
# ---------------------------------------------------------------------------
def name_key(name: str | None) -> str | None:
    """'Kevin O'Brien' -> 'kevin obrien'; used to compare names with email local parts."""
    if not name:
        return None
    cleaned = re.sub(r"[^a-z\s]", "", name.lower().replace("-", " "))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned in PLACEHOLDER_NAMES:
        return None
    return cleaned


def name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    return re.sub(r"\s+", " ", re.sub(r"[._\-]+", " ", local)).strip().lower()


def display_name(key: str) -> str:
    return " ".join(part.capitalize() for part in key.split())


def email_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def is_internal_email(email: str) -> bool:
    return email_domain(email) in INTERNAL_DOMAINS


def domain_stem(domain: str) -> str:
    return domain.split(".", 1)[0]


def compact(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def clean_attendees(raw: object, issues: list[DataIssue]) -> tuple[list[str], list[str]]:
    """Return (valid_emails, unresolved_entries). Obvious obfuscations are repaired."""
    if raw is None:
        issues.append(DataIssue("attendees", "warning", "Attendee list missing"))
        return [], []
    if not isinstance(raw, list):
        issues.append(DataIssue("attendees", "error", "Attendee list is not a list"))
        return [], []
    if not raw:
        issues.append(DataIssue("attendees", "warning", "Attendee list is empty"))

    valid: list[str] = []
    unresolved: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            issues.append(DataIssue("attendees", "warning", f"Blank/non-string attendee entry {entry!r} ignored"))
            continue
        candidate = entry.strip().lower()
        if EMAIL_RE.match(candidate):
            valid.append(candidate)
            continue
        repaired = candidate.replace("[at]", "@").replace("(at)", "@").replace("[dot]", ".").replace("(dot)", ".")
        if EMAIL_RE.match(repaired):
            issues.append(DataIssue("attendees", "warning", f"Malformed email '{entry}' repaired to '{repaired}'"))
            valid.append(repaired)
            continue
        issues.append(DataIssue("attendees", "warning", f"'{entry}' is not an email address; kept as an unresolved attendee"))
        unresolved.append(entry.strip())

    # De-duplicate while keeping order.
    return list(dict.fromkeys(valid)), unresolved


def company_matches_domain(company: str | None, domain_or_stem: str) -> bool:
    """Heuristic: 'Meridian Capital' ~ 'meridiancap', 'Atlas Ventures' ~ 'atlasvc'."""
    comp = compact(company)
    stem = compact(domain_stem(domain_or_stem))
    if not comp or not stem:
        return False
    if stem in comp or comp in stem:
        return True
    prefix = 0
    for a, b in zip(comp, stem):
        if a != b:
            break
        prefix += 1
    return prefix >= 5 and prefix >= 0.6 * len(stem)


# ---------------------------------------------------------------------------
# Text / location
# ---------------------------------------------------------------------------
def _stem(token: str) -> str:
    # Deliberately crude, deterministic stemming: "investment" ~ "invest", "updates" ~ "update".
    return token[:6]


def tokens(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        text = URL_RE.sub(" ", text.lower())
        for tok in re.split(r"[^a-z0-9]+", text):
            if not tok or tok in STOPWORDS:
                continue
            for expanded in ABBREVIATIONS.get(tok, [tok]):
                if expanded not in STOPWORDS:
                    out.add(_stem(expanded))
    return out


def text_similarity(a: set[str], b: set[str]) -> float:
    """Blend of overlap-coefficient (handles subset titles) and Dice (penalises tiny overlaps)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    containment = inter / min(len(a), len(b))
    dice = 2 * inter / (len(a) + len(b))
    return round(0.7 * containment + 0.3 * dice, 4)


def location_kind(location: str | None) -> str | None:
    if not location:
        return None
    low = location.lower()
    return "virtual" if any(k in low for k in VIRTUAL_KEYWORDS) else "in_person"


def normalise_location(location: str | None) -> str:
    text = URL_RE.sub(" ", (location or "").lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compare_locations(a: str | None, b: str | None) -> tuple[float, str]:
    """Return (score, relation) where relation is one of
    unknown | same | compatible | variant | conflict | format_conflict."""
    if not a or not b:
        return 0.5, "unknown"
    kind_a, kind_b = location_kind(a), location_kind(b)
    if kind_a != kind_b:
        return 0.0, "format_conflict"
    na, nb = normalise_location(a), normalise_location(b)
    if na == nb:
        return 1.0, "same"
    if na and nb and (na in nb or nb in na):
        return 0.9, "compatible"   # one is a more specific version of the other
    overlap = text_similarity(tokens(a), tokens(b))
    if overlap >= 0.5:
        return 0.6, "variant"
    return 0.0, "conflict"


def normalise_format(meeting_type: str | None) -> tuple[str | None, str | None]:
    """CRM meeting_type -> (meeting_format, category)."""
    low = (meeting_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if low == "in_person":
        return "in_person", None
    if low == "virtual":
        return "virtual", None
    if low == "internal":
        return None, "internal"
    return None, None
