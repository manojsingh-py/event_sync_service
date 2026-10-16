"""Central configuration. Everything tunable lives here and can be overridden by env vars."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where the two upstream "API" payloads live.
DATA_DIR = Path(os.getenv("EVENT_SYNC_DATA_DIR", PROJECT_ROOT / "data"))
CRM_FILE = DATA_DIR / "crm_events.json"
CALENDAR_FILE = DATA_DIR / "calendar_events.json"

FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Most timestamps in both sources are naive (no offset). The firm's offices are
# DC / NYC / Boston, so we interpret naive times as US Eastern. Explicit offsets
# (e.g. "...Z") are honoured and converted.
LOCAL_TZ_NAME = os.getenv("EVENT_SYNC_LOCAL_TZ", "America/New_York")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

# Email domains that belong to "our" firm; anything else is treated as a client.
INTERNAL_DOMAINS = {d.strip().lower() for d in os.getenv("EVENT_SYNC_INTERNAL_DOMAINS", "firma.com").split(",")}

# ---- Matching (cross-source) -------------------------------------------------
# Weighted evidence model. Weights sum to 1.0, so the score is in [0, 1].
MATCH_WEIGHTS = {
    "time": 0.30,      # how close the start times are (same local date is a hard gate)
    "people": 0.25,    # client / attendee overlap
    "title": 0.25,     # subject vs title token similarity
    "location": 0.10,  # location / meeting format agreement
    "owner": 0.10,     # CRM relationship owner is organizer/attendee in calendar
}
MATCH_THRESHOLD = float(os.getenv("EVENT_SYNC_MATCH_THRESHOLD", "0.55"))
LOW_CONFIDENCE_THRESHOLD = 0.70        # matches below this are flagged for review
MATCH_TIME_WINDOW_MIN = 240            # time score decays to 0 at 4h apart

# ---- Duplicate detection (same source) ---------------------------------------
DUPLICATE_WEIGHTS = {"time": 0.30, "people": 0.30, "title": 0.20, "org": 0.20}
DUPLICATE_THRESHOLD = float(os.getenv("EVENT_SYNC_DUPLICATE_THRESHOLD", "0.70"))
DUPLICATE_TIME_WINDOW_MIN = 120

# ---- Server ------------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
