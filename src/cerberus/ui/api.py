"""JSON views over the database for the UI (see cerberus.ui).

Every function takes a read-only sqlite3 connection: the UI never writes.
Bank series follow the same rules as the analysis code: a timestamp counts
only when every pack reported (pack_count), bank current is the sum of packs,
bank SoC and voltage are the mean of packs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import tzinfo

from .. import findings as findings_mod
from ..detectors import KV_LAST_FULL_CHARGE
from ..live import KV_SNAPSHOT
from ..location import local

# Range -> (seconds back, bucket seconds). Buckets keep every range to a few
# hundred points, which is all a phone-width chart can show.
RANGES: dict[str, tuple[int | None, int]] = {
    "24h": (86400, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 7200),
    "all": (None, 86400),
}
# A snapshot older than this means the collector is not running.
SNAPSHOT_STALE_S = 60
# Data older than this is "not live" even if the collector is up (Cerbo off
# the network, packs out of BLE range).
LIVE_STALE_S = 180
# An RPM reading older than this means the engine is off (the OBD reader polls
# every 2 s while it runs and goes quiet when it stops).
ENGINE_STALE_S = 30
# Below this speed with the engine running, the van is idling.
IDLE_MPH = 2.0
# SoC is a coulomb count that only re-syncs at a true full charge; on this
# rig it drifted ~15 points in a few weeks (2026-10-21). Past this many days
# without one, the UI says the figure may read high.
SOC_TRUST_DAYS = findings_mod.Settings.soc_trust_days


@dataclass(frozen=True)
class Context:
    pack_count: int = 1
    capacity_ah: float | None = None
    tz: tzinfo | None = None
    tz_name: str | None = None
    path_limit_mohm: float = findings_mod.Settings.path_limit_mohm
    full_remind_days: float = findings_mod.Settings.full_remind_days
    def_low_pct: float = findings_mod.Settings.def_low_pct
    service_due_mi: float = findings_mod.Settings.service_due_mi

    def findings_settings(self) -> findings_mod.Settings:
        return findings_mod.Settings(tz=self.tz, path_limit_mohm=self.path_limit_mohm,
                                     full_remind_days=self.full_remind_days,
                                     def_low_pct=self.def_low_pct,
                                     service_due_mi=self.service_due_mi)


def _kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def soc_trust(last_full_ts: int | None, now: int) -> dict:
    if last_full_ts is None:
        return {"trusted": False, "days_since_full": None,
                "note": "No true full charge on record yet, so SoC may read high."}
    days = (now - last_full_ts) / 86400
    if days <= SOC_TRUST_DAYS:
        return {"trusted": True, "days_since_full": round(days, 1),
                "note": f"Last true full charge {days:.0f} days ago."}
    return {"trusted": False, "days_since_full": round(days, 1),
            "note": f"Last true full charge {days:.0f} days ago, so SoC may read high."}


def time_to_empty_h(soc: float | None, current: float | None,
                    capacity_ah: float | None) -> float | None:
    """Hours at the present draw until the bank is empty, or None when it is
    charging, idle, or the capacity is unknown."""
    if soc is None or current is None or not capacity_ah or current > -0.5:
        return None
    return round(soc / 100.0 * capacity_ah / -current, 1)


def now_view(conn: sqlite3.Connection, ctx: Context, now: int | None = None,
             notes: list[dict] | None = None) -> dict:
    now = int(time.time()) if now is None else now
    raw = _kv(conn, KV_SNAPSHOT)
    snap = json.loads(raw) if raw else None
    collector_up = bool(snap) and now - snap.get("ts", 0) <= SNAPSHOT_STALE_S
    bank = (snap or {}).get("bank")
    live = bool(bank) and now - bank.get("ts", 0) <= LIVE_STALE_S
    last_full = _kv(conn, KV_LAST_FULL_CHARGE)
    open_ = [f for f in findings_mod.derive(conn, ctx.findings_settings(), now, notes)
             if f.status == findings_mod.OPEN]
    out = {
        "now": now,
        "tz": ctx.tz_name,
        "collector_up": collector_up,
        "cerbo_online": bool(snap and snap.get("cerbo_online")) and collector_up,
        "live": live,
        "snapshot": snap,
        "soc_trust": soc_trust(int(last_full) if last_full else None, now),
        "time_to_empty_h": None,
        "cabin": None,
        "outside": None,

        # Open findings only, briefly; /api/findings has the full cards.
        # Ones the owner marked known are counted, not listed.
        "findings": [
            {k: f.as_dict()[k] for k in ("key", "severity", "title", "first_seen")}
            for f in open_ if f.known is None
        ],
        "known_count": sum(f.known is not None for f in open_),
    }
    if bank:
        out["time_to_empty_h"] = time_to_empty_h(
            bank.get("soc"), bank.get("current"), bank.get("capacity_ah") or ctx.capacity_ah)
    row = conn.execute(
        "SELECT ts, air_temp_f, humidity_pct, source FROM cabin_samples "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        out["cabin"] = {"ts": row[0], "air_temp_f": row[1], "humidity_pct": row[2],
                        "source": row[3]}
    row = conn.execute(
        "SELECT ts, air_temp_f, humidity_pct FROM weather_samples "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        out["outside"] = {"ts": row[0], "air_temp_f": row[1], "humidity_pct": row[2]}
    return out


def _latest_obd(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT ts, value FROM obd_samples WHERE name = ? AND value IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()


def _latest_vehicle(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT ts, value FROM vehicle_samples WHERE name = ? AND value IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()


def _latest_level(conn: sqlite3.Connection, name: str):
    """(ts, value, source) from whichever is newer: the OBD reader (only while
    the engine runs) or an imported snapshot (cerberus.vehicle; also parked)."""
    cands = [(*r, src) for r, src in ((_latest_obd(conn, name), "obd"),
                                      (_latest_vehicle(conn, name), "import")) if r]
    return max(cands, key=lambda c: c[0]) if cands else None


def vehicle_view(conn: sqlite3.Connection, now: int) -> dict | None:
    """The chassis side: chassis battery voltage (from the ACR, which reads it
    every few seconds), engine state from OBD, and fuel, DEF and odometer from
    OBD or an imported snapshot, whichever is newer. None when the van reports
    none of it."""
    out: dict = {}
    row = conn.execute(
        "SELECT ts, chassis_v_cv, field7 FROM acr_samples "
        "WHERE chassis_v_cv IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    if row and now - row[0] <= LIVE_STALE_S:
        # field7 = 1 while the ACR joins chassis and house (then both read the same).
        out["chassis"] = {"ts": row[0], "voltage": round(row[1] / 100.0, 2),
                          "linked": row[2] == 1}
    rpm = _latest_obd(conn, "rpm")
    if rpm:
        speed = _latest_obd(conn, "speed_mph")
        running = now - rpm[0] <= ENGINE_STALE_S and rpm[1] > 0
        state = "off"
        if running:
            state = "driving" if speed and speed[1] >= IDLE_MPH else "idling"
        out["engine"] = {"ts": rpm[0], "state": state, "rpm": rpm[1] if running else None,
                         "speed_mph": speed[1] if running and speed else None}
    for key, name in (("fuel", "fuel_level_pct"), ("def", "def_level_pct")):
        r = _latest_level(conn, name)
        if r:
            out[key] = {"ts": r[0], "pct": round(r[1], 0), "source": r[2]}
    r = _latest_level(conn, "odometer_mi")
    if r:
        out["odometer"] = {"ts": r[0], "mi": round(r[1]), "source": r[2]}
    r = _latest_vehicle(conn, "range_mi")
    if r and "fuel" in out:
        out["fuel"]["range_mi"] = round(r[1])
    return out or None


# Findings about the chassis rather than the house system: shown on the
# Chassis tab as well as Findings.
CHASSIS_FINDINGS = ("engine_faults", "def_low", "starter_battery", "tyre_pressure",
                    "service_due", "vehicle_fluids")
# starter_battery_state (cerberus.vehicle): Mercedes coding.
STARTER_LABEL = {0: "good", 4: "good to fair", 1: "fair", 3: "weak", 2: "poor"}


def chassis_view(conn: sqlite3.Connection, ctx: Context, now: int | None = None,
                 notes: list[dict] | None = None) -> dict:
    """The Chassis tab: engine, chassis battery, fuel, DEF and odometer
    (vehicle_view), plus what an import adds (tyres, starter battery, fluid
    warnings, lock), the chassis findings and the last few drives."""
    now = int(time.time()) if now is None else now
    out: dict = {"now": now, "tz": ctx.tz_name, "vehicle": vehicle_view(conn, now)}

    def latest(name):
        r = _latest_vehicle(conn, name)
        return None if r is None else {"ts": r[0], "value": r[1]}

    tyres = {pos: latest(f"tire_{pos}_psi") for pos in ("fl", "fr", "rl", "rr")}
    out["tyres"] = tyres if any(tyres.values()) else None
    if out["tyres"] is not None:
        w = latest("tire_warning")
        out["tyres"]["warning"] = None if w is None else bool(w["value"])
        for name, (_, _, med) in findings_mod.low_tyres(conn, now).items():
            tyre = tyres[name.split("_")[1]]
            if tyre is not None:
                tyre["low_from"] = med
    sb = latest("starter_battery_state")
    out["starter_battery"] = None if sb is None else {
        "ts": sb["ts"], "state": int(sb["value"]),
        "label": STARTER_LABEL.get(int(sb["value"]), "unknown")}
    warnings = {}
    for name in ("coolant_low", "brake_fluid_low", "washer_low", "engine_light"):
        r = latest(name)
        if r is not None:
            warnings[name] = {"ts": r["ts"], "on": bool(r["value"])}
    out["warnings"] = warnings or None
    lock = latest("lock_state")
    out["lock_state"] = lock
    found = findings_mod.derive(conn, ctx.findings_settings(), now, notes)
    out["findings"] = [{k: f.as_dict()[k] for k in ("key", "severity", "status", "title", "first_seen")}
                       for f in found if f.key in CHASSIS_FINDINGS]
    drives = []
    for start, end, source, raw in conn.execute(
            "SELECT start_ts, end_ts, source, metrics FROM drives ORDER BY start_ts DESC LIMIT 8"):
        try:
            m = json.loads(raw) if raw else {}
        except ValueError:
            m = {}
        drives.append({"start_ts": start, "end_ts": end, "source": source,
                       "miles": m.get("odometer_driven_mi", m.get("distance_mi")),
                       "alt_ah": m.get("alt_ah"), "peak_bank_v": m.get("peak_bank_v"),
                       "coolant_max_c": m.get("coolant_max_c")})
    out["drives"] = drives
    return out


def findings_view(conn: sqlite3.Connection, ctx: Context, now: int | None = None,
                  notes: list[dict] | None = None) -> dict:
    """Findings with the owner's notes on them; notes whose finding is no
    longer shown come back as other_notes, newest first, so none is lost."""
    now = int(time.time()) if now is None else now
    notes = notes or []
    found = findings_mod.derive(conn, ctx.findings_settings(), now, notes)
    shown = {f.key for f in found}
    return {"now": now, "tz": ctx.tz_name,
            "findings": [f.as_dict() for f in found],
            "other_notes": sorted((n for n in notes if n["key"] not in shown),
                                  key=lambda n: -n["ts"])}


def _utc_offset(tz: tzinfo | None, now: int) -> int:
    """The van's UTC offset in seconds now (host local when tz is None)."""
    off = local(now, tz).astimezone().utcoffset() if tz is None else local(now, tz).utcoffset()
    return int(off.total_seconds()) if off else 0


def _since(now: int, rng: str) -> tuple[int, int]:
    back, bucket = RANGES[rng]
    return (0 if back is None else now - back), bucket


def history_view(conn: sqlite3.Connection, ctx: Context, rng: str,
                 now: int | None = None) -> dict:
    if rng not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    now = int(time.time()) if now is None else now
    since, bucket = _since(now, rng)
    # Buckets start on van-local boundaries (a day bucket at local midnight,
    # not UTC's), using today's offset; a DST change shifts one day by an hour.
    off = _utc_offset(ctx.tz, now)
    # Interpolated, not bound: both are ints from this module (RANGES and the
    # zone offset), never from the request.
    t = f"((ts + {int(off)}) / {int(bucket)}) * {int(bucket)} - {int(off)}"
    out: dict = {"range": rng, "bucket_s": bucket, "since": since, "now": now, "tz": ctx.tz_name}

    out["bank"] = [list(r) for r in conn.execute(
        f"""
        WITH b AS (
          SELECT ts, AVG(soc) soc, AVG(voltage) v, SUM(current) i
          FROM battery_samples WHERE ts >= ? AND pack_id > 0
          GROUP BY ts HAVING COUNT(*) = ?
        )
        SELECT {t} t, ROUND(AVG(soc), 1), ROUND(AVG(v), 3), ROUND(AVG(i), 2)
        FROM b GROUP BY t ORDER BY t
        """, (since, ctx.pack_count))]
    out["solar_w"] = [list(r) for r in conn.execute(
        f"SELECT {t} t, ROUND(AVG(pv_power), 0) FROM solar_samples "
        "WHERE ts >= ? AND pv_power IS NOT NULL GROUP BY t ORDER BY t", (since,))]
    out["ac_w"] = [list(r) for r in conn.execute(
        f"SELECT {t} t, ROUND(AVG(consumption_w), 0) FROM ac_samples "
        "WHERE ts >= ? AND consumption_w IS NOT NULL GROUP BY t ORDER BY t", (since,))]
    out["cabin_f"] = [list(r) for r in conn.execute(
        f"SELECT {t} t, ROUND(AVG(air_temp_f), 1) FROM cabin_samples "
        "WHERE ts >= ? AND air_temp_f IS NOT NULL GROUP BY t ORDER BY t", (since,))]
    out["outside_f"] = [list(r) for r in conn.execute(
        f"SELECT {t} t, ROUND(AVG(air_temp_f), 1) FROM weather_samples "
        "WHERE ts >= ? AND air_temp_f IS NOT NULL GROUP BY t ORDER BY t", (since,))]
    out["daily_yield_kwh"] = daily_yield(conn, ctx, since)
    return out


def daily_yield(conn: sqlite3.Connection, ctx: Context, since: int) -> list[list]:
    """[van-local date, kWh] per day: the MPPT's own daily counter at its
    highest, which is the day's total (it resets at midnight)."""
    by_day: dict[str, float] = {}
    for t, kwh in conn.execute(
            "SELECT (ts / 3600) * 3600 t, MAX(yield_today_kwh) FROM solar_samples "
            "WHERE ts >= ? AND yield_today_kwh IS NOT NULL GROUP BY t", (since,)):
        day = local(int(t), ctx.tz).date().isoformat()
        by_day[day] = max(by_day.get(day, 0.0), float(kwh))
    return [[d, round(by_day[d], 2)] for d in sorted(by_day)]
