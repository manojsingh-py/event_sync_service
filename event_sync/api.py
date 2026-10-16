"""REST API + static frontend.

Note: there is no authentication. The server binds to 127.0.0.1 by default;
put it behind an authenticating proxy before exposing it on a network.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .ingest import IngestError
from .logging_config import get_logger, setup_logging
from .models import CALENDAR, CRM
from .service import ServiceUnavailable, SyncService

setup_logging()
log = get_logger(__name__)

service = SyncService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # If the first sync fails, keep the server up: /api/* returns 503 with the reason
    # and POST /api/sync can be retried once the source files are fixed.
    try:
        service.sync()
    except Exception as exc:  # noqa: BLE001
        log.error("Initial sync failed; API will return 503 until a sync succeeds: %s", exc)
    yield


app = FastAPI(
    title="Event Sync Service",
    description="Unified, reconciled view of CRM meetings and calendar events with per-field provenance.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        log.info("%s %s -> %d (%.1f ms)", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - started) * 1000)
    return response


@app.exception_handler(ServiceUnavailable)
async def unavailable_handler(_: Request, exc: ServiceUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the log; the client only gets a generic message.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/api/health")
def health():
    if not service.has_result:
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": service.last_error})
    return {"status": "ok" if service.last_error is None else "degraded",
            "synced_at": service.result.synced_at, "last_error": service.last_error}


@app.get("/api/stats")
def stats() -> dict:
    return service.result.stats


@app.get("/api/meetings")
def list_meetings(
    source: Literal["both", "crm", "calendar"] | None = Query(None, description="both = matched in both sources; crm/calendar = only in that source"),
    conflicts: bool = Query(False, description="only meetings with medium/high conflicts"),
    needs_review: bool = Query(False, description="only meetings flagged for human review"),
    q: str | None = Query(None, max_length=200, description="case-insensitive text search"),
) -> dict:
    items = service.list_meetings(source=source, conflicts_only=conflicts, needs_review=needs_review, q=q)
    return {"count": len(items), "items": items}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str) -> dict:
    meeting = service.get_meeting(meeting_id)
    if meeting is None:
        log.debug("Meeting %s not found", meeting_id)
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    return meeting


@app.get("/api/sources/{source}")
def get_source(source: Literal["crm", "calendar"]) -> dict:
    records = service.source_records(CRM if source == "crm" else CALENDAR)
    return {"source": source, "count": len(records), "items": records}


@app.post("/api/sync")
def resync() -> dict:
    """Re-read both upstream files and rebuild the reconciled view."""
    try:
        result = service.sync()
    except IngestError as exc:
        # Upstream data problem: report it, keep serving the previous result.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result.stats


# Mounted last so /api/* routes take precedence.
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
