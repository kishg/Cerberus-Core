"""Daily solar charge-path resistance and early-float check.

Why this exists
---------------
The MPPT regulates on the voltage at its own output terminals, not at the
packs. Every milliohm between the two makes it see absorption voltage early,
run out its absorption timer, and float a bank that is not full. That cost
every sunny afternoon until the 2026-09-19 rewire took the path from 130 to
<=50 mOhm. On 09-30, 10-02 and 10-03 it was floating at ~94% again: 10-03
entered absorption with the packs at 13.37 V and ~13 A against a 14.4 V
setpoint, i.e. roughly 80 mOhm, inferred. A joint that has crept back up once
will do it again, so this measures it every sunny day.

Measurement
-----------
solar_samples.dc_voltage (MPPT terminals, since 2026-10-03) against the mean
pack voltage at the same timestamp, over samples where the MPPT pushes at least
MIN_AMPS. Two estimates:

* ``path_mohm`` — median of (V_mppt - V_pack) / I. Always available, but a
  fixed calibration offset between the two devices biases it (50 mV is 4 mOhm
  at 13 A).
* ``path_mohm_slope`` — slope of (V_mppt - V_pack) on I across the day. The
  offset drops out, but it needs the current to vary, which a clear morning's
  ramp provides and a flat overcast day does not.

Samples are dropped while the alternator or shore charger is also charging:
those currents share part of the path, so the drop would no longer be the solar
current's alone.

A third estimate needs no dc_voltage, so it covers history: the MPPT only
leaves bulk when its own terminals reach ABSORPTION_V, so at that moment
``path_mohm_at_absorption`` = (ABSORPTION_V - V_pack) / I. Backfilled, it shows
the path at ~55 mOhm on 09-21..23 (already enough to float at 84-88%) rising to
~80-88 on 10-02/03 — the drift this check exists for.

Early float is recorded from charger state alone, so it backfills over all
history: the first float of the day with the bank below FULL_SOC_PCT is the
symptom this module exists to catch.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo

from . import baseline, drives
from .db import Db
from .location import day_start, local, next_day
from .events import Event, Severity, make

log = logging.getLogger(__name__)

MIN_AMPS = 10.0            # below this the drop is lost in calibration noise
MIN_SAMPLES = 10           # 30 s samples: five minutes of real current
SLOPE_MIN_SPREAD_A = 5.0   # current range needed for the slope to mean anything
FULL_SOC_PCT = 98.0        # floating below this is an early float
# Post-rewire measured <=50 mOhm, pre-rewire 130. Halfway-ish: clearly worse
# than the rewire left it, well before it costs as much as it used to.
ABS_PATH_MOHM = 75.0
# A day is processed once the sun is down. 21:00 local clears sunset all year.
PROCESS_AFTER_HOUR = 21

# The MPPT's absorption setpoint, read off the unit by the user 2026-10-03
# (float 13.4 V; DVCC off, so nothing overrides it). Used only for the inferred
# estimate below. If it is ever changed on the charger, change it here.
ABSORPTION_V = 14.4

STATE_ABSORPTION = 4
STATE_FLOAT = 5


@dataclass(frozen=True)
class Settings:
    """Per-van values from config.yaml `solar_path`. The module constants
    above are the defaults."""
    absorption_v: float = ABSORPTION_V
    path_limit_mohm: float = ABS_PATH_MOHM
    process_after_hour: int = PROCESS_AFTER_HOUR
    # From config's bank section: rows per whole-bank sample, and the net
    # charge current above which another charger shares the path.
    pack_count: int = 1
    charge_on_a: float = 20.0
    # The van's zone: what a "day" is, and when 21:00 is. None = host local.
    # The collector sets it per tick (config, else the GX's own setting).
    tz: tzinfo | None = None


DEFAULT_SETTINGS = Settings()

KV_THROUGH = "solar_path.processed_through_day"
EVENT_TYPE = "solar_path_report"
BASELINE_METRICS = {
    "path_mohm_at_absorption": "(absorption setpoint - V_pack) / I when absorption began",
    "path_mohm": "median (V_mppt - V_pack) / I at >= 10 A",
    "path_mohm_slope": "slope of (V_mppt - V_pack) on I",
}


def _day_bounds(day: str, tz: tzinfo | None = None) -> tuple[int, int]:
    return day_start(day, tz), day_start(next_day(day), tz)


def _hhmm(ts: int | None, tz: tzinfo | None = None) -> str | None:
    return None if ts is None else local(ts, tz).strftime("%H:%M")


def compute_day(db: Db, day: str, settings: Settings = DEFAULT_SETTINGS) -> dict | None:
    """Metrics for one local calendar day, or None if the MPPT never ran."""
    start, end = _day_bounds(day, settings.tz)
    solar = db.conn.execute(
        "SELECT ts, state, charge_current, dc_voltage, yield_today_kwh "
        "FROM solar_samples WHERE ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
    if not solar or not any((r[2] or 0) > 0 for r in solar):
        return None
    elec = {r[0]: r for r in baseline.electrical_series(db, start, end, settings.pack_count)}

    m: dict = {
        "peak_solar_a": round(max(r[2] or 0 for r in solar), 1),
        "yield_kwh": max((r[4] for r in solar if r[4] is not None), default=None),
    }

    first = {}
    for ts, state, *_ in solar:
        if state in (STATE_ABSORPTION, STATE_FLOAT) and state not in first:
            first[state] = ts
    m["absorption_at"] = _hhmm(first.get(STATE_ABSORPTION), settings.tz)
    if STATE_ABSORPTION in first:
        at = first[STATE_ABSORPTION]
        amps_at = next((r[2] for r in solar if r[0] >= at and r[2] is not None), None)
        # How full the bank already was: a day that starts absorption nearly
        # full floats near full whatever the cause (findings.early_float).
        soc_near = [t for t in elec if abs(t - at) <= 120]
        if soc_near:
            m["soc_at_absorption"] = round(elec[min(soc_near, key=lambda t: abs(t - at))][3], 1)
        near = [t for t in elec if abs(t - at) <= 60]
        if near and amps_at is not None and amps_at >= MIN_AMPS:
            e = elec[min(near, key=lambda t: abs(t - at))]
            # Same exclusions as the measured path: another charger sharing
            # the path would inflate the pack-side voltage drop's meaning.
            if e[2] is not None and not e[6] and e[5] <= settings.charge_on_a:
                m["path_mohm_at_absorption"] = round(
                    (settings.absorption_v - e[2]) / amps_at * 1000, 1
                )
                m["absorption_amps"] = round(amps_at, 1)
    if STATE_FLOAT in first:
        ft = first[STATE_FLOAT]
        m["float_at"] = _hhmm(ft, settings.tz)
        near = [t for t in elec if abs(t - ft) <= 120]
        if near:
            e = elec[min(near, key=lambda t: abs(t - ft))]
            m["soc_at_float"] = round(e[3], 1)
            m["pack_v_at_float"] = round(e[2], 2)

    pairs = []
    for ts, _state, amps, v_mppt, _y in solar:
        if amps is None or amps < MIN_AMPS or v_mppt is None:
            continue
        e = elec.get(ts)
        if e is None or e[2] is None:
            continue
        _ts, _bank_a, v_pack, _soc, _solar_a, alt_a, shore = e
        if shore or alt_a > settings.charge_on_a:
            continue
        pairs.append((amps, v_mppt - v_pack))
    m["path_samples"] = len(pairs)
    if len(pairs) >= MIN_SAMPLES:
        m["path_mohm"] = round(
            statistics.median(dv / i for i, dv in pairs) * 1000, 1
        )
        m["path_amps_median"] = round(statistics.median(i for i, _ in pairs), 1)
        amps = [i for i, _ in pairs]
        if max(amps) - min(amps) >= SLOPE_MIN_SPREAD_A:
            sl = baseline._slope(amps, [dv for _, dv in pairs])
            if sl is not None:
                m["path_mohm_slope"] = round(sl * 1000, 1)
    return {k: v for k, v in m.items() if v is not None}


def evaluate(m: dict, history: list[dict],
             settings: Settings = DEFAULT_SETTINGS) -> list[dict]:
    flags: list[dict] = []
    limit_mohm = settings.path_limit_mohm
    for name in ("path_mohm", "path_mohm_at_absorption"):
        if m.get(name) is not None and m[name] >= limit_mohm:
            flags.append({"kind": "limit", "metric": name,
                          "value": m[name], "limit": f">= {limit_mohm} mOhm"})
    soc = m.get("soc_at_float")
    if soc is not None and soc < FULL_SOC_PCT:
        flags.append({"kind": "limit", "metric": "soc_at_float", "value": soc,
                      "limit": f"< {FULL_SOC_PCT}% (early float at {m.get('float_at')})"})
    for name in BASELINE_METRICS:
        x = m.get(name)
        hist = [h[name] for h in history if h.get(name) is not None]
        if x is None or len(hist) < baseline.MIN_BASELINE:
            continue
        z, med, _ = baseline.robust_z(float(x), hist)
        if abs(z) >= baseline.Z_FLAG:
            flags.append({"kind": "anomaly", "metric": name, "value": x,
                          "baseline": round(med, 1), "z": round(z, 1),
                          "n": len(hist)})
    return flags


def summary_line(day: str, m: dict, flags: list[dict]) -> str:
    parts = [f"Solar {day[5:]}"]
    if "path_mohm_at_absorption" in m:
        parts.append(f"path ~{m['path_mohm_at_absorption']:.0f} mOhm at absorption "
                     f"({m['absorption_amps']:.0f} A)")
    if "path_mohm" in m:
        p = f"path {m['path_mohm']:.0f} mOhm"
        if "path_mohm_slope" in m:
            p += f" (slope {m['path_mohm_slope']:.0f})"
        parts.append(p + f" at {m['path_amps_median']:.0f} A")
    elif "path_mohm_at_absorption" not in m:
        parts.append(f"path n/a (peak {m.get('peak_solar_a', 0):.0f} A)")
    if "float_at" in m:
        parts.append(f"float {m['float_at']} at {m.get('soc_at_float', '?')}%")
    head = ", ".join(parts)
    if not flags:
        return f"{head} — normal"
    bits = []
    for f in flags:
        if f["kind"] == "anomaly":
            bits.append(f"{f['metric']} {f['value']} vs {f['baseline']} (z={f['z']})")
        else:
            bits.append(f"{f['metric']} {f['value']} {f['limit']}")
    return f"{head} — " + "; ".join(bits)


def prior_days(db: Db, day: str, epochs=baseline.DEFAULT_EPOCHS,
               tz: tzinfo | None = None) -> list[dict]:
    start, _ = _day_bounds(day, tz)
    since = local(baseline.epoch_start(start, epochs, tz), tz).strftime("%Y-%m-%d")
    return [
        json.loads(r[0]) for r in db.conn.execute(
            "SELECT metrics FROM solar_days WHERE day >= ? AND day < ? ORDER BY day",
            (since, day),
        )
    ]


def record_day(db: Db, day: str, epochs=baseline.DEFAULT_EPOCHS,
               settings: Settings = DEFAULT_SETTINGS) -> tuple[dict, list] | None:
    m = compute_day(db, day, settings)
    if m is None:
        return None
    flags = evaluate(m, prior_days(db, day, epochs, settings.tz), settings)
    db.conn.execute(
        "INSERT OR REPLACE INTO solar_days (day, metrics, flags, created_ts) "
        "VALUES (?, ?, ?, ?)",
        (day, json.dumps(m, sort_keys=True), json.dumps(flags), int(time.time())),
    )
    return m, flags


def _days_to_process(db: Db, now: int, through: str | None,
                     after_hour: int = PROCESS_AFTER_HOUR,
                     tz: tzinfo | None = None) -> list[str]:
    today = local(now, tz)
    last = today.date() if today.hour >= after_hour else today.date() - timedelta(days=1)
    if through is None:
        row = db.conn.execute("SELECT MIN(ts) FROM solar_samples").fetchone()
        if row is None or row[0] is None:
            return []
        d = local(row[0], tz).date()
    else:
        d = date.fromisoformat(next_day(through))
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def process(db: Db, now: int, epochs=baseline.DEFAULT_EPOCHS,
            settings: Settings = DEFAULT_SETTINGS) -> list[Event]:
    """Record every finished day not yet recorded.

    First run backfills silently (early floats over all history). After that,
    each day yields one event: info when normal, warn when flagged — HA pushes
    only the warnings.
    """
    through = db.get_kv(KV_THROUGH)
    first_run = through is None
    events: list[Event] = []
    for day in _days_to_process(db, now, through, settings.process_after_hour, settings.tz):
        res = record_day(db, day, epochs, settings)
        db.set_kv(KV_THROUGH, day)
        if res is None or first_run:
            continue
        m, flags = res
        events.append(make(
            EVENT_TYPE, Severity.WARN if flags else Severity.INFO,
            {"summary": summary_line(day, m, flags), "day": day, "flags": len(flags)},
            ts=now,
        ))
    return events
