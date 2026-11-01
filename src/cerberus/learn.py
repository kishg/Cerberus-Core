"""Setup learning: settings proposed from the van's own history.

Detection (cerberus.detect) covers what the Cerbo can state. Some settings
only show up once the van has run for a while; this module learns them from
the database and proposes them in the same shape, so the Settings screen
offers "Use" for both. Until there is enough history, a setting is
*learning*, with a progress line, rather than a guess ("forming is honest").

* **solar.array_voc_v.** Each day's highest PV voltage while the charge
  controller draws nothing (pv_power < 5 W): the array at open circuit. The
  median over recent days, so one cold morning (this rig: 21.9 V against a
  ~20 V usual) or a cloudy day doesn't set it.
* **drives.engine_charge_a.** Charge sessions from the engine, computed from
  raw current (bank minus solar, not on shore), not from detected drives:
  drive detection is sized by this setting, so learning from it would only
  confirm whatever was set. Sessions must last 5 minutes and start below 90%
  SoC, so the bank could take what was offered. Sessions are bimodal (this
  rig: 130-200 A from the alternator against 5-20 A of morning solar the
  controller under-reports), so only the upper cluster counts: sessions
  reaching at least half of the largest. The setting is the 90th percentile
  of their 95th-percentile currents.
* **drives.joint_temp_instance.** The temperature probe that heats while the
  engine charges: the largest median rise over those sessions, if it is
  clearly a rise (>= 5 C) and clearly the largest. This is how instance 24
  was placed on the alternator path (2026-09-24). Rise correlates only
  loosely with mean I² (r 0.5 here) because session length matters as much,
  so rise alone is the test.

Not learned yet: heater thresholds. heater.py already finds kilowatt-class
AC runs with generic defaults; naming the appliance needs an "ask" step.
"""

from __future__ import annotations

import sqlite3
import statistics

from . import drives as drives_mod

VOC_DAYS = 30
VOC_MIN_DAYS = 7
VOC_IDLE_W = 5.0
VOC_DAY_MIN_V = 10.0           # a day whose idle PV never reached this had no sun

ENGINE_DAYS = 90
ENGINE_MIN_SESSIONS = 3
SESSION_ON_A = 5.0
SESSION_MIN_S = 300
SESSION_MAX_SOC = 90.0
UPPER_FRAC = 0.5

JOINT_MIN_RISE_C = 5.0
JOINT_MARGIN = 2.0             # the joint's rise is at least this many times the next probe's

# Learned numbers within this of the current value count as matching: they
# move a little day to day and shouldn't nag.
MATCH_TOLERANCE = 0.05


class _Conn:
    """drives.electrical_series takes a Db; the UI holds a bare connection."""

    def __init__(self, conn):
        self.conn = conn


def _learned(value, reason, display=None):
    return {"value": value, "reason": reason, "display": display, "together": [],
            "learned": True}


def _learning(reason):
    return {"value": None, "reason": reason, "display": None, "together": [],
            "learned": True, "learning": True}


def _pct(sorted_xs: list[float], q: float) -> float:
    return sorted_xs[int(q * (len(sorted_xs) - 1))]


def array_voc(conn: sqlite3.Connection, now: int) -> dict | None:
    try:
        rows = conn.execute(
            "SELECT ts / 86400 AS d, MAX(pv_voltage) FROM solar_samples "
            "WHERE ts >= ? AND source = 'mqtt' AND COALESCE(pv_power, 0) < ? "
            "AND pv_voltage IS NOT NULL GROUP BY d",
            (now - VOC_DAYS * 86400, VOC_IDLE_W)).fetchall()
    except sqlite3.Error:
        return None
    days = [v for _, v in rows if v is not None and v >= VOC_DAY_MIN_V]
    if not rows:
        return None                 # no charge controller data at all: nothing to say
    if len(days) < VOC_MIN_DAYS:
        return _learning(f"Learning from sunny days: {len(days)} of {VOC_MIN_DAYS} so far.")
    v = round(statistics.median(days), 1)
    return _learned(v, f"Median of the highest open-circuit panel voltage on the last "
                       f"{len(days)} sunny days (range {min(days):.1f}-{max(days):.1f} V).")


def engine_sessions(conn: sqlite3.Connection, now: int, pack_count: int) -> list[dict]:
    """Engine-like charge sessions, independent of any engine setting."""
    series = drives_mod.electrical_series(_Conn(conn), now - ENGINE_DAYS * 86400, now,
                                          pack_count)
    sessions: list[dict] = []
    for ts, _i, _v, soc, _solar, alt, shore in series:
        if alt <= SESSION_ON_A or shore:
            continue
        if sessions and ts - sessions[-1]["end"] <= drives_mod.MERGE_GAP_S:
            sessions[-1]["end"] = ts
            sessions[-1]["amps"].append(alt)
        else:
            sessions.append({"start": ts, "end": ts, "amps": [alt], "soc": soc})
    out = []
    for s in sessions:
        if s["end"] - s["start"] < SESSION_MIN_S or s["soc"] is None or s["soc"] >= SESSION_MAX_SOC:
            continue
        s["p95"] = _pct(sorted(s["amps"]), 0.95)
        out.append(s)
    if not out:
        return []
    top = max(s["p95"] for s in out)
    return [s for s in out if s["p95"] >= top * UPPER_FRAC]


def engine_charge(sessions: list[dict]) -> dict:
    n = len(sessions)
    if n < ENGINE_MIN_SESSIONS:
        return _learning(f"Learning from drives with the batteries below {SESSION_MAX_SOC:.0f}%: "
                         f"{n} of {ENGINE_MIN_SESSIONS} so far.")
    p = sorted(s["p95"] for s in sessions)
    a = int(round(_pct(p, 0.9) / 5.0) * 5)
    return _learned(a, f"From {n} drives that charged the batteries "
                       f"(typical {statistics.median(p):.0f} A, highest {p[-1]:.0f} A).")


def joint_probe(conn: sqlite3.Connection, sessions: list[dict]) -> dict | None:
    try:
        instances = [r[0] for r in conn.execute(
            "SELECT DISTINCT instance FROM temperature_samples").fetchall()]
    except sqlite3.Error:
        return None
    if not instances:
        return None                 # no temperature probes on this GX
    if len(sessions) < ENGINE_MIN_SESSIONS:
        return _learning(f"Learning which probe heats while driving: {len(sessions)} of "
                         f"{ENGINE_MIN_SESSIONS} drives so far.")
    rises: dict[int, list[float]] = {}
    for s in sessions:
        for inst in instances:
            t = conn.execute(
                "SELECT temperature_c FROM temperature_samples WHERE instance = ? "
                "AND ts BETWEEN ? AND ? AND temperature_c IS NOT NULL ORDER BY ts",
                (inst, s["start"] - 120, s["end"])).fetchall()
            if len(t) >= 3:
                rises.setdefault(inst, []).append(max(x for (x,) in t) - t[0][0])
    med = {i: statistics.median(r) for i, r in rises.items() if len(r) >= ENGINE_MIN_SESSIONS}
    if not med:
        return _learning("Learning which probe heats while driving: not enough probe "
                         "readings during drives yet.")
    best = max(med, key=med.get)
    others = [v for i, v in med.items() if i != best]
    if med[best] < JOINT_MIN_RISE_C or (others and med[best] < JOINT_MARGIN * max(others)):
        return _learned(None, "No probe heats clearly with charging current, so none is "
                              "treated as a joint on the charge path.", display="None")
    return _learned(best, f"Probe {best} rises {med[best]:.0f} °C (median) while the engine "
                          f"charges, over {len(rises[best])} drives: it sits on the charge path.")


def proposals(conn: sqlite3.Connection, now: int, pack_count: int) -> dict:
    out = {}
    voc = array_voc(conn, now)
    if voc:
        out["solar.array_voc_v"] = voc
    try:
        sessions = engine_sessions(conn, now, pack_count)
    except sqlite3.Error:
        return out
    out["drives.engine_charge_a"] = engine_charge(sessions)
    joint = joint_probe(conn, sessions)
    if joint:
        out["drives.joint_temp_instance"] = joint
    return out
