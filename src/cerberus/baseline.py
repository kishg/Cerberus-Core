"""Analysis shared by the drive reports, the daily solar check and setup learning.

Nothing here is about vehicles: the bank's electrical series, the robust
baseline comparison and hardware epochs apply to any LiFePO4 + MPPT +
inverter system. Moved out of drives.py (docs/site_agnostic_plan.md, PR 1) so
solar_path and learn no longer depend on the drive module; drives re-exports
every name, so ``drives.<name>`` still works.

Baseline rule: robust z-score against prior history, (x - median) /
(1.4826 * MAD), flagged at |z| >= Z_FLAG. Below MIN_BASELINE comparable
values a metric is "baseline forming" and never flagged.
"""

from __future__ import annotations

import statistics
from datetime import tzinfo

from .db import Db
from .location import day_start

SHORE_INPUT_W = 50.0         # AC input above this while shore-connected = charger

MIN_BASELINE = 8
Z_FLAG = 3.5

# Hardware changes that make earlier history incomparable. Values before the
# latest epoch at or before a given time are excluded from its baseline.
# Per site, so none by default; set them in drives.baseline_epochs (this rig's
# 2026-09-19 DC rewire lives there).
DEFAULT_EPOCHS: tuple[str, ...] = ()


def electrical_series(db: Db, since: int, until: int,
                      pack_count: int = 1) -> list[tuple]:
    """(ts, bank_a, bank_v, soc, solar_a, alt_a, shore_charging) per sample.

    Only timestamps where every pack reported, because battery_samples is per
    pack and a partial row would understate bank current (by a third, on this
    rig's three packs).
    """
    rows = db.conn.execute(
        """
        WITH b AS (
          SELECT ts, SUM(current) i, AVG(voltage) v, AVG(soc) soc, COUNT(*) n
          FROM battery_samples
          WHERE source = 'mqtt' AND ts BETWEEN ? AND ?
          GROUP BY ts
        )
        SELECT b.ts, b.i, b.v, b.soc, s.charge_current, a.input_w, a.shore_connected
        FROM b
        LEFT JOIN solar_samples s ON s.ts = b.ts
        LEFT JOIN ac_samples a ON a.ts = b.ts
        WHERE b.n = ?
        ORDER BY b.ts
        """,
        (since, until, pack_count),
    ).fetchall()
    out = []
    for ts, i, v, soc, solar, input_w, shore in rows:
        if i is None:
            continue
        solar = solar or 0.0
        shore_charging = bool(shore == 1 and (input_w or 0) > SHORE_INPUT_W)
        out.append((ts, i, v, soc, solar, i - solar, shore_charging))
    return out


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _integrate(points: list[tuple[int, float]], max_dt: int = 60) -> float:
    """Left Riemann sum in value·hours, not bridging gaps longer than max_dt."""
    total = 0.0
    for (t0, v0), (t1, _v1) in zip(points, points[1:]):
        dt = t1 - t0
        if 0 < dt <= max_dt:
            total += v0 * dt / 3600.0
    return total


def epoch_start(before_ts: int, epochs=DEFAULT_EPOCHS, tz: tzinfo | None = None) -> int:
    """Start of the hardware epoch containing before_ts (0 if none)."""
    starts = [day_start(e, tz) for e in epochs]
    return max((t for t in starts if t <= before_ts), default=0)


def robust_z(x: float, history: list[float]) -> tuple[float, float, float]:
    """(z, median, scaled MAD). A zero MAD is floored at 2% of the median so a
    perfectly flat history does not turn every tiny change into infinity."""
    med = statistics.median(history)
    mad = statistics.median(abs(h - med) for h in history) * 1.4826
    mad = max(mad, abs(med) * 0.02, 1e-9)
    return (x - med) / mad, med, mad
