"""Retrospective decode of Frame A fields 5 and 6 from captured BLE CSVs.

Per-pack hypotheses from prior observations:
  - field 5 differed across packs *under charge*; uniform at rest
  - field 6 differed across packs *under discharge*; uniform under charge
We test those by binning every Frame A sample by current sign and looking
for stable per-pack correlations with current magnitude, voltage, SoC, temp.

  python tools/ble_field_decode.py

Reads from ble_captures/continuous/*.csv and ble_captures/pulse_*.csv.
Pure stdlib — no pandas.
"""
from __future__ import annotations

import csv
import glob
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sample:
    ts: float
    pack: int
    pack_v_cv: int
    cell1_cv: int
    cell2_cv: int
    cell3_cv: int
    cell4_cv: int
    field5: int
    field6: int
    current_a: int
    soc: int
    flags: str
    # Frame B (joined by closest-in-time)
    temp_dc: int | None = None
    big_counter: int | None = None
    config_hex: str | None = None


def parse_frame_a(line: str) -> tuple | None:
    s = line.strip()
    if not s or s.startswith("&"):
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 10:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]),
                int(parts[4]), int(parts[5]), int(parts[6]), int(parts[7]),
                int(parts[8]), parts[9])
    except ValueError:
        return None


def parse_frame_b(line: str) -> tuple | None:
    s = line.strip()
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 8 or parts[0] != "&":
        return None
    try:
        return (int(parts[1]), int(parts[2]), int(parts[3]), parts[6])
    except (ValueError, IndexError):
        return None


def iter_rows(path: str):
    """Yield (ts: float, pack: int, line: str) from a continuous BLE CSV."""
    with open(path) as f:
        next(f, None)
        for raw in f:
            parts = raw.rstrip("\n").split(",", 2)
            if len(parts) < 3:
                continue
            try:
                ts = float(parts[0])
                pid = int(parts[1])
            except ValueError:
                continue
            yield ts, pid, parts[2]


def load_samples(paths: list[str]) -> list[Sample]:
    """Load and join Frame A + nearest-in-time Frame B per pack."""
    # First pass: collect raw (ts, pack, parsed_a or parsed_b)
    a_rows: list[tuple[float, int, tuple]] = []
    b_rows_by_pack: dict[int, list[tuple[float, tuple]]] = defaultdict(list)
    for p in paths:
        for ts, pack, line in iter_rows(p):
            a = parse_frame_a(line)
            if a is not None:
                a_rows.append((ts, pack, a))
                continue
            b = parse_frame_b(line)
            if b is not None:
                # b[0] is BMS-reported pack id; use the *line* pack hint to key on
                # since the address mapping in the file may already match
                b_rows_by_pack[pack].append((ts, b))

    # Sort per-pack B lists for binary search
    for pid in b_rows_by_pack:
        b_rows_by_pack[pid].sort(key=lambda x: x[0])

    import bisect
    def nearest_b(pack: int, ts: float) -> tuple | None:
        rows = b_rows_by_pack.get(pack)
        if not rows:
            return None
        idx = bisect.bisect_left([r[0] for r in rows], ts)
        candidates = []
        if idx > 0:
            candidates.append(rows[idx - 1])
        if idx < len(rows):
            candidates.append(rows[idx])
        # tolerance ±5s
        candidates = [c for c in candidates if abs(c[0] - ts) <= 5.0]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c[0] - ts))[1]

    samples: list[Sample] = []
    for ts, pack, a in a_rows:
        pv, c1, c2, c3, c4, f5, f6, cur, soc, flags = a
        b = nearest_b(pack, ts)
        temp = big = cfg = None
        if b is not None:
            _, temp, big, cfg = b
        samples.append(Sample(
            ts=ts, pack=pack,
            pack_v_cv=pv, cell1_cv=c1, cell2_cv=c2, cell3_cv=c3, cell4_cv=c4,
            field5=f5, field6=f6, current_a=cur, soc=soc, flags=flags,
            temp_dc=temp, big_counter=big, config_hex=cfg,
        ))
    return samples


def bin_label(current: int) -> str:
    if current >= 5:
        return "charge_heavy(>=5A)"
    if current >= 1:
        return "charge_light(1-4A)"
    if current == 0:
        return "rest(0A)"
    if current >= -4:
        return "discharge_light(-1..-4A)"
    return "discharge_heavy(<=-5A)"


def summarize_per_pack_field(samples: list[Sample], field_name: str) -> None:
    """Stats per pack per current-bin, for the given field."""
    by_pack_bin: dict[tuple[int, str], list[int]] = defaultdict(list)
    for s in samples:
        v = getattr(s, field_name)
        by_pack_bin[(s.pack, bin_label(s.current_a))].append(v)

    bins = ["charge_heavy(>=5A)", "charge_light(1-4A)", "rest(0A)",
            "discharge_light(-1..-4A)", "discharge_heavy(<=-5A)"]
    print(f"\n=== {field_name} by pack × current bin (mean / range / n) ===")
    print(f"  {'bin':<26} {'pack1':<22} {'pack2':<22} {'pack3':<22}")
    for b in bins:
        cells = []
        for pid in (1, 2, 3):
            vals = by_pack_bin.get((pid, b), [])
            if vals:
                cells.append(f"{statistics.mean(vals):5.1f} [{min(vals)}-{max(vals)}] n={len(vals)}")
            else:
                cells.append("(none)")
        print(f"  {b:<26} {cells[0]:<22} {cells[1]:<22} {cells[2]:<22}")


def cross_pack_delta(samples: list[Sample], field_name: str) -> None:
    """For samples where we have all 3 packs at near-same timestamp, compute
    pack3 - mean(pack1,pack2) for the field."""
    by_ts_pack: dict[float, dict[int, int]] = defaultdict(dict)
    # Bucket to nearest 5 seconds
    for s in samples:
        bucket = round(s.ts / 5.0) * 5.0
        by_ts_pack[bucket][s.pack] = getattr(s, field_name)

    deltas_by_bin: dict[str, list[float]] = defaultdict(list)
    for bucket, packs in by_ts_pack.items():
        if not all(p in packs for p in (1, 2, 3)):
            continue
        avg12 = (packs[1] + packs[2]) / 2.0
        delta = packs[3] - avg12
        # Need the current for binning — pick the pack 3 value at that bucket
        sample_at_bucket = next((s for s in samples
                                  if s.pack == 3 and round(s.ts / 5.0) * 5.0 == bucket), None)
        if sample_at_bucket is None:
            continue
        deltas_by_bin[bin_label(sample_at_bucket.current_a)].append(delta)

    print(f"\n=== pack3 - mean(pack1,pack2) for {field_name}, by current bin ===")
    bins = ["charge_heavy(>=5A)", "charge_light(1-4A)", "rest(0A)",
            "discharge_light(-1..-4A)", "discharge_heavy(<=-5A)"]
    for b in bins:
        vs = deltas_by_bin.get(b, [])
        if not vs:
            print(f"  {b:<26}  (no aligned samples)")
            continue
        print(f"  {b:<26}  mean Δ={statistics.mean(vs):+5.2f}  "
              f"median={statistics.median(vs):+5.1f}  "
              f"range=[{min(vs):+.0f},{max(vs):+.0f}]  n={len(vs)}")


def main() -> int:
    paths = sorted(glob.glob("ble_captures/continuous/ble_*.csv") +
                   glob.glob("ble_captures/pulse_*.csv"))
    if not paths:
        print("no BLE captures found in ble_captures/")
        return 1
    print(f"loading {len(paths)} capture files...")
    for p in paths:
        print(f"  {p}")
    samples = load_samples(paths)
    print(f"\nparsed {len(samples)} Frame A samples")

    by_pack = defaultdict(int)
    for s in samples:
        by_pack[s.pack] += 1
    print(f"per-pack counts: {dict(by_pack)}")

    # Range of distinct field values
    f5_vals = sorted(set(s.field5 for s in samples))
    f6_vals = sorted(set(s.field6 for s in samples))
    print(f"\nfield 5 distinct values: {f5_vals}")
    print(f"field 6 distinct values: {f6_vals}")

    for fname in ("field5", "field6"):
        summarize_per_pack_field(samples, fname)
        cross_pack_delta(samples, fname)

    # Correlate field 5/6 with continuous variables — Pearson r
    def pearson(xs, ys):
        if len(xs) < 3:
            return None
        mx = statistics.mean(xs); my = statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = sum((x - mx) ** 2 for x in xs) ** 0.5
        sy = sum((y - my) ** 2 for y in ys) ** 0.5
        if sx == 0 or sy == 0:
            return None
        return num / (sx * sy)

    def fmt_r(r):
        return f"{r:+.2f}" if r is not None else " n/a "

    for fname in ("field5", "field6"):
        print(f"\n=== Pearson r per pack — {fname} vs (current, |current|, V, SoC, temp) ===")
        for pid in (1, 2, 3):
            ss = [s for s in samples if s.pack == pid]
            if len(ss) < 50:
                continue
            ff = [getattr(s, fname) for s in ss]
            cur = [s.current_a for s in ss]
            acur = [abs(s.current_a) for s in ss]
            v = [s.pack_v_cv for s in ss]
            soc = [s.soc for s in ss]
            ss_t = [s for s in ss if s.temp_dc is not None]
            ff_t = [getattr(s, fname) for s in ss_t]
            temp_t = [s.temp_dc for s in ss_t]
            print(f"  pack {pid} (n={len(ss)}):  "
                  f"r(cur)={fmt_r(pearson(ff,cur))}  "
                  f"r(|cur|)={fmt_r(pearson(ff,acur))}  "
                  f"r(V)={fmt_r(pearson(ff,v))}  "
                  f"r(SoC)={fmt_r(pearson(ff,soc))}  "
                  f"r(T)={fmt_r(pearson(ff_t,temp_t))}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
