"""Print a reconciliation report to the terminal: `python -m event_sync.cli [--json out.json] [--log-level DEBUG]`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ingest import IngestError
from .logging_config import get_logger, setup_logging
from .reconcile import summarise
from .service import run_sync

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the event sync pipeline once and print a report.")
    parser.add_argument("--json", type=Path, help="also write the full reconciled output to this file")
    parser.add_argument("--log-level", default=None, help="DEBUG shows every candidate score and data issue")
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    try:
        result = run_sync()
    except IngestError as exc:
        log.error("Cannot run sync: %s", exc)
        return 2

    print(json.dumps({k: v for k, v in result.stats.items() if k != "settings"}, indent=2))
    print()
    for m in result.meetings:
        s = summarise(m)
        when = (s["start"] or s["date"] or "?")[:16].replace("T", " ")
        ids = "+".join(s["source_ids"]["crm"] + s["source_ids"]["calendar"])
        score = f"{s['match_score']:.2f}" if s["match_score"] is not None else "  - "
        flags = ",".join(f"{c['field']}:{c['severity']}" for c in m["conflicts"])
        print(f"{when:16}  {score}  {ids:24}  {(s['title'] or '')[:40]:40}  {flags}")

    if args.json:
        try:
            args.json.write_text(json.dumps(result.meetings, indent=2), encoding="utf-8")
        except OSError as exc:
            log.error("Could not write %s: %s", args.json, exc)
            return 1
        print(f"\nWrote {len(result.meetings)} meetings to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
