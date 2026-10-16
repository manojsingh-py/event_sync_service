"""Logging setup shared by the server, the CLI and the tests.

Level is controlled by EVENT_SYNC_LOG_LEVEL (DEBUG, INFO, WARNING, ...; default INFO).
DEBUG shows per-record parsing issues and every candidate match score, which is
the quickest way to see why two records did or did not link.
"""
from __future__ import annotations

import logging
import os

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured = False


def setup_logging(level: str | int | None = None) -> None:
    """Configure the `event_sync` logger tree once. Safe to call repeatedly."""
    global _configured
    resolved = level or os.getenv("EVENT_SYNC_LOG_LEVEL", "INFO")
    if isinstance(resolved, str):
        resolved = logging.getLevelName(resolved.upper())
        if not isinstance(resolved, int):  # unknown name -> fall back to INFO
            resolved = logging.INFO

    root = logging.getLogger("event_sync")
    root.setLevel(resolved)
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
        # propagate stays True so pytest's caplog (attached to the root logger) sees records;
        # the root logger has no handlers under uvicorn, so nothing is printed twice.
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("event_sync") else f"event_sync.{name}")
