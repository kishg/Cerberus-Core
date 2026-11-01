"""Compare two Cerberus boxes reading the same van, before retiring the old one.

    .venv/bin/python tools/compare_boxes.py http://localhost:8088 http://cerberus-van:8088 [--minutes 10]

Polls both boxes' /api/v1/now every 15 s and reports, per reading, how far
apart they were. Two boxes on the same Cerbo should agree to within what moves
between two polls a second or so apart: bank SoC exactly, voltage to a few
hundredths, currents and watts within the noise of a live load. Anything
beyond the limits below, or one box stale or offline, is flagged.
Read-only: GET requests only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# (path in /now's snapshot, label, allowed difference)
READINGS = [
    (("bank", "soc"), "bank SoC %", 0.5),
    (("bank", "voltage"), "bank V", 0.05),
    (("bank", "current"), "bank A", 3.0),
    (("solar", "pv_w"), "solar W", 15.0),
    (("ac", "consumption_w"), "AC load W", 30.0),
]
POLL_S = 15


def fetch(base: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + "/api/v1/now", timeout=10) as r:
        return json.loads(r.read())


def pick(d: dict, path):
    for k in path:
        d = (d or {}).get(k)
    return d


def compare(a: dict, b: dict) -> list[tuple[str, float | None, float | None, bool]]:
    """(label, a, b, ok) per reading; a missing side is a failure unless both are."""
    out = []
    for path, label, limit in READINGS:
        va, vb = pick(a.get("snapshot"), path), pick(b.get("snapshot"), path)
        if va is None and vb is None:
            continue
        ok = va is not None and vb is not None and abs(va - vb) <= limit
        out.append((label, va, vb, ok))
    for flag in ("collector_up", "cerbo_online"):
        if a.get(flag) != b.get(flag):
            out.append((flag, a.get(flag), b.get(flag), False))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--minutes", type=float, default=10)
    args = ap.parse_args()
    end = time.time() + args.minutes * 60
    worst: dict[str, float] = {}
    bad = polls = 0
    while time.time() < end:
        try:
            a, b = fetch(args.old), fetch(args.new)
        except Exception as err:
            print(f"{time.strftime('%H:%M:%S')}  poll failed: {err}")
            bad += 1
        else:
            polls += 1
            for label, va, vb, ok in compare(a, b):
                if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                    worst[label] = max(worst.get(label, 0.0), abs(va - vb))
                if not ok:
                    bad += 1
                    print(f"{time.strftime('%H:%M:%S')}  {label}: old {va}  new {vb}")
        time.sleep(POLL_S)
    print(f"\n{polls} polls; largest differences:")
    for label, d in worst.items():
        print(f"  {label}: {d:.2f}")
    print("AGREE" if bad == 0 else f"{bad} disagreement(s): look before retiring the old box")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
