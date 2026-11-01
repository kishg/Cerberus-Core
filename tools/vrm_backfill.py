#!/usr/bin/env python3
"""Import a VRM export into battery_samples to close a collector outage.

Why this exists
---------------
The Cerbo uploads to VRM independently of this laptop, so VRM survives every
outage Cerberus has (asleep/shut down, wrong network, stale-DNS wedge). When
`battery_samples` has a hole, VRM is the only record of what the bank did.

Provenance, and why these rows are NOT pack rows
------------------------------------------------
VRM's history API returns a *single system-level series*. The `instance`
parameter is accepted but ignored by `/stats` — instances 1, 2 and 3 return
byte-identical data under every attribute-code form (`bv/bc/bs`, `V/I/SOC`).
Per-instance values exist only on `/widgets/BatterySummary`, which serves the
latest reading and no history. Verified 2026-08-30.

So there is no per-pack history to import. Writing this series to pack_id 1/2/3
would fabricate a three-way breakdown that VRM never recorded and would poison
any current-share or capacity-asymmetry work spanning the window. Instead these
land on **pack_id 0**, which the MQTT collector never writes, tagged
`source='vrm'`.

Query accordingly:
  * per-pack analysis      -> WHERE pack_id IN (1,2,3)   (equivalently source='mqtt')
  * gap-spanning SoC/volt  -> pack_id 0 fills the hole, at 15-minute resolution

Resolution: the Cerbo's loggingInterval is 900 s, so every value is a 15-minute
bucket average, against 30 s samples on the mqtt path. Real peaks are higher
than these rows show — do not use them for peak-current or taper claims.

Solar and AC (--table solar|ac, added 2026-10-04)
-------------------------------------------------
These tables have one MPPT and one VE.Bus, so there is no per-pack problem and
rows go in at the bucket ts tagged `source='vrm'`. CSV columns:

  solar: pvp, pvv, sci, scv, yt   (VRM PVP, PVV, ScI, ScV, YT)
  ac:    s                        (VRM S, the VE.Bus state)

Deliberately left NULL:
  * solar `state` — VRM's ScS is a bucket average of an enum, so a bucket that
    straddles a transition reads nonsense (124 seen on 10-04). solar_path keys
    its absorption/float times off this column; a fake state would move them.
  * solar `error_code` — not exported.
  * ac power columns — `ac_loads` and the per-phase codes return no records for
    this installation. With the inverter off the collector writes them NULL
    too (only the VE.Bus state is real), so `s` alone matches a live row.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys


def _reader(path: str):
    with open(path, newline="") as fh:
        yield from csv.DictReader(r for r in fh if not r.startswith("#"))


def _num(r: dict, k: str) -> float | None:
    return float(r[k]) if r.get(k) not in (None, "") else None


def battery_rows(path: str, first_ts: int, step: int):
    for i, r in enumerate(_reader(path)):
        f = lambda k: _num(r, k)
        yield (
            first_ts + i * step,
            0,                  # pack_id 0 = VRM system view, never written by MQTT
            f("soc"),
            f("bv"),
            f("bc"),
            f("bp_w"),
            None,               # temperature: not in this export
            None,               # timetogo: not in this export
            "vrm",
        )


def solar_rows(path: str, first_ts: int, step: int):
    for i, r in enumerate(_reader(path)):
        f = lambda k: _num(r, k)
        yield (
            first_ts + i * step,
            f("pvp"),
            f("pvv"),
            f("sci"),
            f("yt"),
            None,               # state: bucket-averaged enum, see module docstring
            None,               # error_code: not exported
            f("scv"),
            "vrm",
        )


def ac_rows(path: str, first_ts: int, step: int):
    for i, r in enumerate(_reader(path)):
        s = _num(r, "s")
        yield (
            first_ts + i * step,
            None, None, None,   # consumption/input/output: no VRM records
            None,               # shore_connected: not exported
            None if s is None else int(s),
            "vrm",
        )


# table -> (row builder, INSERT, column that marks VRM rows in the clash check)
TABLES = {
    "battery": (
        battery_rows,
        "INSERT OR REPLACE INTO battery_samples "
        "(ts, pack_id, soc, voltage, current, power, temperature, timetogo, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "SELECT COUNT(*) FROM battery_samples WHERE pack_id = 0 AND ts BETWEEN ? AND ?",
    ),
    "solar": (
        solar_rows,
        "INSERT OR REPLACE INTO solar_samples "
        "(ts, pv_power, pv_voltage, charge_current, yield_today_kwh, state, "
        "error_code, dc_voltage, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "SELECT COUNT(*) FROM solar_samples WHERE ts BETWEEN ? AND ?",
    ),
    "ac": (
        ac_rows,
        "INSERT OR REPLACE INTO ac_samples "
        "(ts, consumption_w, input_w, output_w, shore_connected, inverter_state, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        "SELECT COUNT(*) FROM ac_samples WHERE ts BETWEEN ? AND ?",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    ap.add_argument("--table", choices=sorted(TABLES), default="battery")
    ap.add_argument("--db", required=True)
    ap.add_argument("--first-ts", type=int, required=True,
                    help="unix ts of the first data row (VRM buckets are aligned to :00/:15/:30/:45)")
    ap.add_argument("--step", type=int, default=900)
    ap.add_argument("--commit", action="store_true", help="write; otherwise dry-run")
    a = ap.parse_args()

    build, insert_sql, clash_sql = TABLES[a.table]
    table = insert_sql.split()[4]
    rows = list(build(a.csv_path, a.first_ts, a.step))
    if not rows:
        print("no rows parsed", file=sys.stderr)
        return 1

    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA busy_timeout=5000")
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "source" not in cols:
        print(f"{table} has no `source` column — start the collector once "
              "to run the migration in db.py", file=sys.stderr)
        return 1

    span = (rows[0][0], rows[-1][0])
    clash = conn.execute(clash_sql, span).fetchone()[0]
    print(f"{table}: {len(rows)} rows, ts {span[0]}..{span[1]}, existing rows in span: {clash}")
    if a.table != "battery" and clash:
        # Solar/AC VRM rows share the live rows' key space (no pack_id 0), so
        # OR REPLACE here could overwrite real 30s collector samples. Refuse
        # unless every row in the span is already ours from an earlier run.
        mqtt = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source != 'vrm' AND ts BETWEEN ? AND ?",
            span,
        ).fetchone()[0]
        if mqtt:
            print(f"refusing: {mqtt} collector rows inside the span — trim the window",
                  file=sys.stderr)
            return 1

    if not a.commit:
        for r in rows[:3]:
            print("  would insert:", r)
        print("dry run — pass --commit to write")
        return 0

    # OR REPLACE so re-running a corrected export is idempotent. Battery rows
    # can only ever touch pack_id 0; solar/AC are guarded by the check above.
    with conn:
        conn.executemany(insert_sql, rows)
    print(f"inserted/replaced {len(rows)} {table} rows with source='vrm'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
