"""Cross-reference soak full_charge events with BLE Frame B counters.

For each full_charge event in the soak DB, find the closest Frame B
sample (per pack) before and after the event in the continuous BLE
captures. Prints the counter values on each side so we can see whether
the counter resets at full_charge.

  python ble_correlate_charge.py [window_minutes]
"""
from __future__ import annotations

import datetime
import glob
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_WINDOW_MIN = 15.0
SOAK_DB = Path("soak/data/cerberus.db")
BLE_DIR = Path("ble_captures/continuous")


def parse_frame_b(line: str) -> dict | None:
    s = line.strip()
    if not s.startswith("&"):
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 8:
        return None
    try:
        return {
            "pack_id": int(parts[1]),
            "temp_dc": int(parts[2]),
            "big_counter": int(parts[3]),
        }
    except ValueError:
        return None


def load_ble_b_frames() -> list[tuple[float, int, int]]:
    """Return all Frame B samples across all continuous CSVs as
    (ts, pack_id, big_counter)."""
    rows: list[tuple[float, int, int]] = []
    for path in sorted(glob.glob(str(BLE_DIR / "ble_*.csv"))):
        with open(path) as f:
            next(f, None)  # header
            for line in f:
                # Split only the first two commas — line column contains commas
                parts = line.rstrip("\n").split(",", 2)
                if len(parts) < 3:
                    continue
                try:
                    ts = float(parts[0])
                    pid_hint = int(parts[1])
                except ValueError:
                    continue
                b = parse_frame_b(parts[2])
                if b is None:
                    continue
                rows.append((ts, b["pack_id"], b["big_counter"]))
    return rows


def closest_before_after(samples, target_ts, pack_id, window_s):
    before = None
    after = None
    for ts, pid, counter in samples:
        if pid != pack_id:
            continue
        if ts < target_ts and (target_ts - ts) <= window_s:
            if before is None or ts > before[0]:
                before = (ts, counter)
        elif ts >= target_ts and (ts - target_ts) <= window_s:
            if after is None or ts < after[0]:
                after = (ts, counter)
    return before, after


def main() -> int:
    window_min = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WINDOW_MIN
    window_s = window_min * 60

    if not SOAK_DB.exists():
        print(f"no soak DB at {SOAK_DB}")
        return 1
    conn = sqlite3.connect(SOAK_DB)
    events = list(conn.execute(
        "SELECT ts, detail FROM events WHERE type='full_charge' ORDER BY ts"
    ))
    print(f"found {len(events)} full_charge events in soak DB")
    print(f"BLE window: ±{window_min:g} min around each event")
    print()

    samples = load_ble_b_frames()
    print(f"loaded {len(samples)} BLE Frame B samples across all captures")
    print()

    for ts, detail_json in events:
        detail = json.loads(detail_json) if detail_json else {}
        when = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        socs = detail.get("detail", {}).get("soc", {})
        vmin = detail.get("detail", {}).get("voltage_min")
        print(f"=== full_charge @ {when} (SoC={socs}, V_min={vmin}) ===")
        any_coverage = False
        for pid in (1, 2, 3):
            before, after = closest_before_after(samples, ts, pid, window_s)
            if before or after:
                any_coverage = True
            def fmt(x):
                if x is None:
                    return "  (none)         "
                ts_str = datetime.datetime.fromtimestamp(x[0]).strftime("%H:%M:%S")
                return f"  @{ts_str} → {x[1]:>10d}"
            delta = (after[1] - before[1]) if (before and after) else None
            delta_str = f"  Δ={delta:+d}" if delta is not None else ""
            print(f"  pack {pid}:  before:{fmt(before)}   after:{fmt(after)}{delta_str}")
        if not any_coverage:
            print("  (no BLE coverage in window)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
