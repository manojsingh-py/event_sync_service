"""HTTP API behaviour, including failure modes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from event_sync import api
from event_sync.service import SyncService


@pytest.fixture
def client():
    with TestClient(api.app) as c:  # runs the lifespan (initial sync)
        yield c


@pytest.fixture
def fresh_service(monkeypatch):
    """Swap the module-level service for an isolated one."""

    def _swap(crm_path, cal_path) -> SyncService:
        svc = SyncService(crm_path, cal_path)
        monkeypatch.setattr(api, "service", svc)
        return svc

    return _swap


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["synced_at"]


def test_stats(client):
    s = client.get("/api/stats").json()
    assert s["unified_meetings"] == 24 and s["settings"]["timezone"] == "America/New_York"


@pytest.mark.parametrize("params, expected", [
    ({}, 24),
    ({"source": "both"}, 17),
    ({"source": "crm"}, 3),
    ({"source": "calendar"}, 4),
    ({"conflicts": "true"}, 5),
    ({"q": "pinnacle"}, 1),
    ({"q": "CAL-A16"}, 1),
    ({"q": "zzz-no-match"}, 0),
])
def test_list_filters(client, params, expected):
    body = client.get("/api/meetings", params=params).json()
    assert body["count"] == expected == len(body["items"])


def test_invalid_filter_is_rejected(client):
    assert client.get("/api/meetings", params={"source": "fax"}).status_code == 422


def test_detail_and_404(client):
    first = client.get("/api/meetings").json()["items"][0]
    detail = client.get(f"/api/meetings/{first['id']}").json()
    assert detail["id"] == first["id"] and "fields" in detail and "records" in detail
    assert client.get("/api/meetings/MTG-missing").status_code == 404


def test_sources_endpoint(client):
    body = client.get("/api/sources/calendar").json()
    assert body["count"] == 22 and all(item["meeting_id"] for item in body["items"])
    assert client.get("/api/sources/email").status_code == 422


def test_resync(client):
    r = client.post("/api/sync")
    assert r.status_code == 200 and r.json()["unified_meetings"] == 24


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Event Sync" in r.text


def test_failed_resync_keeps_previous_result(client, fresh_service, write_sources, tmp_path):
    crm_path, cal_path = write_sources([], [])
    svc = fresh_service(crm_path, cal_path)
    svc.sync()
    cal_path.write_text("{broken", encoding="utf-8")

    r = client.post("/api/sync")
    assert r.status_code == 502 and "Invalid JSON" in r.json()["detail"]
    health = client.get("/api/health").json()
    assert health["status"] == "degraded" and "Invalid JSON" in health["last_error"]
    assert client.get("/api/meetings").status_code == 200  # still serving the old result


def test_no_data_returns_503(client, fresh_service, tmp_path):
    fresh_service(tmp_path / "missing.json", tmp_path / "missing.json")
    assert client.get("/api/meetings").status_code == 503
    assert client.get("/api/health").status_code == 503
    assert client.post("/api/sync").status_code == 502


def test_unexpected_error_returns_generic_500(fresh_service, write_sources, monkeypatch):
    crm_path, cal_path = write_sources([], [])
    svc = fresh_service(crm_path, cal_path)
    svc.sync()

    def boom(*_, **__):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(svc, "list_meetings", boom)
    with TestClient(api.app, raise_server_exceptions=False) as c:
        r = c.get("/api/meetings")
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error"}
