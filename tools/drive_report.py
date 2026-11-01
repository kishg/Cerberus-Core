"""Inspect per-drive records and reports (see cerberus.drives).

    python tools/drive_report.py                 # table of recent drives
    python tools/drive_report.py --all           # every drive
    python tools/drive_report.py --show last     # full report for the latest
    python tools/drive_report.py --show 2026-09-28T09:32
    python tools/drive_report.py --rebuild       # recompute all drives
    python tools/drive_report.py --solar         # daily solar path check

The collector records drives on its own; this is for reading them, and for
--rebuild after a metric or baseline-epoch change. --rebuild writes to the
live database (the drives table and its cursor only) and sends no
notifications. Epochs, the joint probe and the solar-path limits come from
--config, as they do for the collector.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cerberus import config as cfg_mod  # noqa: E402
from cerberus import drives  # noqa: E402
from cerberus import location  # noqa: E402
from cerberus import solar_path  # noqa: E402
from cerberus.db import Db  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
DEFAULT_DB = HERE / "soak" / "data" / "cerberus.db"
DEFAULT_CONFIG = HERE / "config.yaml"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("CERBERUS_SQLITE_PATH", str(DEFAULT_DB)))
    ap.add_argument("--all", action="store_true", help="list every drive")
    ap.add_argument("--show", metavar="WHEN", help="'last' or YYYY-MM-DDTHH:MM")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--solar", action="store_true",
                    help="daily solar path resistance / early-float table")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--epochs", nargs="*",
                    help="override drives.baseline_epochs from --config")
    args = ap.parse_args()
    drives_cfg, solar_settings, tz_name = cfg_mod.load_analysis(args.config)
    if args.epochs is None:
        args.epochs = list(drives_cfg.baseline_epochs)

    db = Db(args.db)
    # The van's zone, resolved as the collector does it: config, else the
    # GX's own setting as the collector last stored it.
    tz = location.VanLocation(db.kv_mapping()).timezone(tz_name)
    drives_settings = replace(drives_cfg.settings, tz=tz)
    solar_settings = replace(solar_settings, tz=tz)
    report_dir = Path(args.db).parent / "reports"

    if args.rebuild:
        n = drives.rebuild(db, int(time.time()), report_dir, epochs=args.epochs,
                           settings=drives_settings)
        db.conn.execute("DELETE FROM solar_days")
        db.conn.execute("DELETE FROM kv WHERE key = ?", (solar_path.KV_THROUGH,))
        solar_path.process(db, int(time.time()), epochs=args.epochs,
                           settings=solar_settings)
        print(f"rebuilt {n} drives and the daily solar table; reports in {report_dir}")
        return 0

    if args.solar:
        limit = "" if args.all else "LIMIT 21"
        rows = db.conn.execute(
            f"SELECT day, metrics, flags FROM solar_days ORDER BY day DESC {limit}"
        ).fetchall()
        for day, metrics, flags in reversed(rows):
            print(solar_path.summary_line(day, json.loads(metrics),
                                          json.loads(flags or "[]")))
        return 0

    if args.show:
        if args.show == "last":
            row = db.conn.execute(
                "SELECT start_ts FROM drives ORDER BY start_ts DESC LIMIT 1"
            ).fetchone()
        else:
            when = datetime.strptime(args.show, "%Y-%m-%dT%H:%M")
            ts = int((when if tz is None else when.replace(tzinfo=tz)).timestamp())
            row = db.conn.execute(
                "SELECT start_ts FROM drives WHERE start_ts BETWEEN ? AND ? "
                "ORDER BY ABS(start_ts - ?) LIMIT 1",
                (ts - 300, ts + 300, ts),
            ).fetchone()
        if row is None:
            print("no such drive", file=sys.stderr)
            return 1
        st, et, src, metrics, flags = db.conn.execute(
            "SELECT start_ts, end_ts, source, metrics, flags FROM drives WHERE start_ts = ?",
            row,
        ).fetchone()
        d = drives.Drive(st, et, src, json.loads(metrics), json.loads(flags or "[]"))
        print(drives.render_report(d, drives.prior_drives(db, st, args.epochs, tz), tz))
        return 0

    limit = "" if args.all else "LIMIT 20"
    rows = db.conn.execute(
        f"SELECT start_ts, source, metrics, flags FROM drives "
        f"ORDER BY start_ts DESC {limit}"
    ).fetchall()
    for st, src, metrics, flags in reversed(rows):
        d = drives.Drive(st, 0, src, json.loads(metrics), json.loads(flags or "[]"))
        print(f"{src[:4]:4}  {drives.summary_line(d, tz)}")
    history = drives.prior_drives(db, int(time.time()) + 1, args.epochs, tz)
    status = drives.baseline_status(history)
    print("\nbaseline (comparable drives since epoch; flags need "
          f"{drives.MIN_BASELINE}):")
    for name, n in status.items():
        print(f"  {name:20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
