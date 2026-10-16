from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from event_sync.service import SyncResult, run_sync  # noqa: E402


@pytest.fixture(scope="session")
def result() -> SyncResult:
    """The pipeline run once against the real assessment data."""
    return run_sync()


@pytest.fixture
def meeting_for(result):
    """Look up the unified meeting that contains a given source record id."""

    def _find(source_id: str) -> dict:
        for m in result.meetings:
            if source_id in m["source_ids"]["crm"] + m["source_ids"]["calendar"]:
                return m
        raise AssertionError(f"{source_id} is not in any meeting")

    return _find


@pytest.fixture
def write_sources(tmp_path):
    """Write small CRM/calendar payloads to temp files and return their paths."""

    def _write(crm: object, calendar: object) -> tuple[Path, Path]:
        crm_path, cal_path = tmp_path / "crm.json", tmp_path / "cal.json"
        crm_path.write_text(json.dumps(crm) if not isinstance(crm, str) else crm, encoding="utf-8")
        cal_path.write_text(json.dumps(calendar) if not isinstance(calendar, str) else calendar, encoding="utf-8")
        return crm_path, cal_path

    return _write
