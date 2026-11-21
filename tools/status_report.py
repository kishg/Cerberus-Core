"""Status table (cerberus.status): bank/pack/cell levels with 24 h change,
cabin and outside temperature/humidity range, solar peak and yield, last 24 h
vs the 24 h before, then the owner's recent notes on findings.

    python tools/status_report.py --url http://cerberus:8088   # a box, over its API
    python tools/status_report.py [--db soak/data/cerberus.db] [--config config.yaml]

--url reads GET /api/v1/status from a running Cerberus: no database access
needed, so it works from any machine that reaches the box. --db reads a
database directly (read-only), with the notes from beside --config.
Prints GitHub markdown. Temperatures: ambient in F.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cerberus import notes, status


def _zone(name):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="a running Cerberus, e.g. http://cerberus:8088")
    ap.add_argument("--db", default="soak/data/cerberus.db")
    ap.add_argument("--config", default="config.yaml",
                    help="with --db: the owner's notes are read from beside this file")
    args = ap.parse_args()
    if args.url:
        with urllib.request.urlopen(args.url.rstrip("/") + "/api/v1/status", timeout=30) as r:
            view = json.loads(r.read())
        print(status.markdown(view, _zone(view.get("tz"))))
        return
    # Read-only: it may run beside the live collector.
    db = sqlite3.connect(f"file:{quote(str(Path(args.db).resolve()))}?mode=ro", uri=True)
    view = status.status_view(db, int(time.time()), notes=notes.read(args.config))
    print(status.markdown(view))


if __name__ == "__main__":
    main()
