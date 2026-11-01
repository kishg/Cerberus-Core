"""Status snapshot for the continuous BLE capture.

  python ble_status.py [seconds]   # default last 120s

Reads ble_captures/continuous/current.csv and prints, per pack:
  - frame counts (last window)
  - latest Frame A (pack V, cell V, SoC, flags)
  - latest Frame B (temp, big counter, fields 5/6, hex bitfield)
  - range of field 7 (current) seen in window — to spot transients
"""
from __future__ import annotations

import csv
import datetime
import statistics
import sys
import time
from pathlib import Path

LOG = Path("ble_captures/continuous/current.csv")


def classify(line: str) -> str:
    s = line.strip()
    if s.startswith("&"):
        return "B"
    if "," in s:
        return "A"
    return "?"


def parse_ints(line: str) -> list:
    out = []
    for p in line.strip().split(","):
        p = p.strip()
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return out


def main() -> int:
    if not LOG.exists():
        print(f"no log at {LOG}")
        return 1
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    cutoff = time.time() - window

    rows: list[tuple[float, int, str]] = []
    with LOG.open() as f:
        next(f)  # header
        for line in f:
            ts_s, pid_s, _, txt = line.partition(",")[0], None, None, None
            parts = line.rstrip("\n").split(",", 2)
            if len(parts) < 3:
                continue
            ts, pid, txt = float(parts[0]), int(parts[1]), parts[2]
            rows.append((ts, pid, txt))

    if not rows:
        print("no rows in log")
        return 0
    print(f"=== BLE status @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"log: {LOG.resolve()}  rows: {len(rows)}  age: {time.time()-rows[-1][0]:.1f}s since last frame")
    print(f"window: last {window:.0f}s")

    for pid in (1, 2, 3):
        in_window = [(ts, txt) for ts, p, txt in rows if p == pid and ts >= cutoff]
        all_p = [(ts, txt) for ts, p, txt in rows if p == pid]
        n_a = sum(1 for _, t in in_window if classify(t) == "A")
        n_b = sum(1 for _, t in in_window if classify(t) == "B")
        last_a = next((t for _, t in reversed(all_p) if classify(t) == "A"), None)
        last_b = next((t for _, t in reversed(all_p) if classify(t) == "B"), None)
        print()
        print(f"--- pack {pid}: A={n_a} B={n_b} in window ---")
        if last_a:
            f = parse_ints(last_a)
            print(f"  Frame A: {last_a}")
            if len(f) >= 10 and all(isinstance(x, int) for x in f[:5] + [f[8]]):
                pv = f[0]/100
                cells = [f[i]/100 for i in (1, 2, 3, 4)]
                print(f"    pack V={pv:.2f}V  cells={cells}  SoC={f[8]}%  flags={f[9]}")
                print(f"    fields5/6/7 (per-pack signature): {f[5]},{f[6]},{f[7]}")
        if last_b:
            f = parse_ints(last_b)
            print(f"  Frame B: {last_b}")
            if len(f) >= 8 and isinstance(f[2], int):
                print(f"    temp={f[2]/10:.1f}°C  counter={f[3]}  fields5/6={f[5],f[6]}")
        # Current-ish field (Frame A field 7) range
        currents = []
        for _, t in in_window:
            if classify(t) == "A":
                p = parse_ints(t)
                if len(p) >= 8 and isinstance(p[7], int):
                    currents.append(p[7])
        if currents:
            print(f"  field[7] (current?) window: min={min(currents)}  max={max(currents)}  nonzero={sum(1 for c in currents if c != 0)}/{len(currents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
