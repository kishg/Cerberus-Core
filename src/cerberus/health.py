"""Data health: is data arriving, and when it didn't, why not.

The first thing an owner does on seeing a hole in a chart is distrust all of
it, so the UI explains holes. Everything here reads the database only.

Freshness is judged per source against its own cadence. Bluetooth sources
(battery packs, ACR) only reach a computer inside the van, so going quiet is
"out of range", not a fault.

A gap is a stretch with no live battery data (source 'mqtt'; rows imported
afterwards from the VRM cloud do not count as collected). Its cause comes
from what *else* was written during it:

* **Cerberus running and online** — weather (every 15 min) and Govee cloud
  cabin rows kept arriving, so only the Cerbo was silent. On this van, 30 of
  the 45 gaps in Oct 2026 looked like this (overnight Cerbo WiFi dropouts).
* **Cerberus running, no internet** — collector_uptime rows but no weather:
  the van's whole network was down. Uptime rows exist since 2026-10-31.
  Before then, Bluetooth rows tell the same story: the BLE sidecar runs on
  the same computer, so cell readings arriving mean it was awake. On
  2026-10-22 it wrote 14,160 cell rows through a 10-hour "nothing recorded"
  gap: the inverter was off, and the Starlink router with it.
* **Nothing written at all** — the computer was asleep or off. A
  collector_started event at the end means it restarted (reboot, power
  loss); none means it slept. Restart markers also exist since 2026-10-31;
  before that the two cannot be told apart, and the explanation says so.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import tzinfo

from .location import day_start, local, next_day

DAY = 86400
GAP_MIN_S = 600           # shorter holes are ordinary jitter, not gaps
BATTERY_CADENCE_S = 30


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    table: str
    where: str            # extra SQL condition
    stale_s: int
    # Bluetooth sources only reach a computer inside the van: quiet means out
    # of range, which is not a fault.
    in_van_only: bool = False
    note: str = ""


SOURCES = (
    Source("battery", "Batteries (Cerbo)", "battery_samples", "source = 'mqtt'", 180),
    Source("solar", "Solar charger (Cerbo)", "solar_samples", "source = 'mqtt'", 180),
    Source("ac", "Inverter (Cerbo)", "ac_samples", "source = 'mqtt'", 180),
    Source("probes", "Temperature probes (Cerbo)", "temperature_samples", "1 = 1", 600),
    Source("cerbo_wifi", "Cerbo WiFi signal", "cerbo_wifi_samples", "1 = 1", 900),
    Source("cabin", "Cabin sensor", "cabin_samples", "1 = 1", 1800),
    Source("weather", "Weather forecast", "weather_samples", "1 = 1", 3600),
    Source("packs_ble", "Battery Bluetooth", "pack_ble_samples", "1 = 1", 600, True,
           "Cell voltages and battery internals. Only reachable inside the van."),
    Source("acr", "Charge relay Bluetooth", "acr_samples", "1 = 1", 600, True,
           "Alternator charging and the chassis battery. Only reachable inside the van."),
    Source("engine", "Engine (OBD)", "engine_samples", "1 = 1", 0, True,
           "Only while driving."),
)


# With the inverter off the Cerbo publishes almost nothing for it (its whole AC
# tree goes null), so rows arrive only every few minutes: that is the inverter
# being off, not the feed failing. Still stale past this.
AC_OFF_STALE_S = 900


def _has_table(conn, name: str) -> bool:
    return _one(conn, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,)) is not None


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def sources(conn: sqlite3.Connection, now: int) -> list[dict]:
    out = []
    for s in SOURCES:
        if not _has_table(conn, s.table):
            continue
        last = _one(conn, f"SELECT MAX(ts) FROM {s.table} WHERE {s.where}")
        if last is None:
            continue                       # never collected on this van: not shown
        age = now - last
        if s.stale_s == 0 or age <= s.stale_s:
            status = "fresh" if s.stale_s else "idle"
        else:
            status = "away" if s.in_van_only else "stale"
        row = {"key": s.key, "label": s.label, "last_ts": last, "age_s": age,
               "status": status, "note": s.note}
        if s.key == "ac" and _one(conn, "SELECT inverter_state FROM ac_samples WHERE source = 'mqtt' "
                                        "ORDER BY ts DESC LIMIT 1") == 0:
            row["note"] = "The inverter is off."
            if age <= AC_OFF_STALE_S:
                row["status"] = "off"
        if s.key == "cabin":
            src = _one(conn, "SELECT source FROM cabin_samples ORDER BY ts DESC LIMIT 1")
            row["note"] = {"ble": "Read over Bluetooth.",
                           "govee_cloud": "Read from the Govee cloud (out of Bluetooth range)."
                           }.get(src, "")
        out.append(row)
    return out


def _battery_ts(conn, since: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT ts FROM battery_samples WHERE ts >= ? AND source = 'mqtt' ORDER BY ts",
        (since,))]


def coverage(conn: sqlite3.Connection, now: int, tz: tzinfo | None, days: int = 7) -> list[list]:
    """[van-local date, % of the day with live battery data] for the last
    `days` days, today counted up to now."""
    today = local(now, tz).date().isoformat()
    day = local(now - (days - 1) * DAY, tz).date().isoformat()
    out = []
    while day <= today:
        a, b = day_start(day, tz), min(day_start(next_day(day), tz), now)
        n = _one(conn, "SELECT COUNT(DISTINCT ts) FROM battery_samples "
                       "WHERE ts >= ? AND ts < ? AND source = 'mqtt'", (a, b)) or 0
        out.append([day, round(min(100.0, 100.0 * n * BATTERY_CADENCE_S / max(b - a, 1)), 1)])
        day = next_day(day)
    return out


def _frac(conn, table, where, a, b, cadence) -> float:
    n = _one(conn, f"SELECT COUNT(*) FROM {table} WHERE ts > ? AND ts < ? AND {where}", (a, b)) or 0
    return n / max((b - a) / cadence, 1)


# Tables the BLE sidecar writes. It is a separate process on the same
# computer, so its rows prove the computer was awake even when the collector
# wrote nothing.
BLE_TABLES = ("pack_ble_samples", "cell_samples")
AWAKE_BUCKET_S = 300


def _awake_frac(conn, a: int, b: int) -> float:
    """Share of the 5-minute slots in (a, b) with a Bluetooth row."""
    buckets: set[int] = set()
    for t in BLE_TABLES:
        if _has_table(conn, t):
            buckets.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT ts / {AWAKE_BUCKET_S} FROM {t} WHERE ts > ? AND ts < ?", (a, b)))
    return len(buckets) / max((b - a) / AWAKE_BUCKET_S, 1)


def explain_gap(conn: sqlite3.Connection, a: int, b: int) -> dict:
    """Cause of a gap from a to b (b = when data resumed, or now)."""
    internet = max(_frac(conn, "weather_samples", "1 = 1", a, b, 900),
                   _frac(conn, "cabin_samples", "source = 'govee_cloud'", a, b, 300))
    # Absent on a database from a collector older than 2026-10-31.
    has_uptime = _has_table(conn, "collector_uptime")
    uptime = _frac(conn, "collector_uptime", "1 = 1", a, b, 300) if has_uptime else 0.0
    uptime_era = _one(conn, "SELECT MIN(ts) FROM collector_uptime") if has_uptime else None
    known = uptime_era is not None and uptime_era <= a
    restarted = _one(conn, "SELECT COUNT(*) FROM events WHERE type = 'collector_started' "
                           "AND ts > ? AND ts <= ?", (a, b + 60)) or 0

    if internet >= 0.5:
        wifi = _one(conn, "SELECT signal_strength FROM cerbo_wifi_samples WHERE ts <= ? "
                          "ORDER BY ts DESC LIMIT 1", (a,))
        text = ("Cerberus was running and online (it kept fetching the weather), but the "
                "Cerbo sent nothing: it was off its network or had lost its connection.")
        if wifi is not None and wifi < 40:
            text += f" Its WiFi signal was {wifi}% just before."
        return {"cause": "cerbo", "title": "The Cerbo wasn't reachable", "explain": text}
    awake = _awake_frac(conn, a, b)
    if known and uptime >= 0.5:
        return _network("Cerberus was running")
    if internet >= 0.2:
        return {"cause": "partial", "title": "Partly the Cerbo, partly Cerberus stopped",
                "explain": ("For part of this time Cerberus was running but the Cerbo sent "
                            "nothing; for the rest, nothing at all was recorded.")}
    if restarted:
        return {"cause": "restart", "title": "Cerberus restarted after being off",
                "explain": ("Nothing was recorded, and Cerberus started up again afterwards: "
                            "the computer running it was off, rebooted, or lost power.")}
    if awake >= 0.5:
        if known:
            return {"cause": "stopped", "title": "Cerberus wasn't running",
                    "explain": ("The computer was awake (Bluetooth readings kept arriving), "
                                "but the collector wasn't running and didn't restart.")}
        return _network("The computer running Cerberus was awake (Bluetooth readings "
                        "kept arriving)",
                        " (Before Oct 31, 2026 a stopped collector looks the same.)")
    if known:
        return {"cause": "asleep", "title": "The computer running Cerberus was asleep",
                "explain": ("Nothing at all was recorded and Cerberus didn't restart "
                            "afterwards, so the computer was asleep. A computer that stays "
                            "awake in the van (the mini PC) avoids this.")}
    return {"cause": "stopped", "title": "Cerberus wasn't collecting",
            "explain": ("Nothing at all was recorded: the computer was asleep or off, or "
                        "Cerberus was stopped. (Gaps before Oct 31, 2026 can't tell these apart.)")}


def _network(who: str, caveat: str = "") -> dict:
    return {"cause": "network", "title": "The van's network was down",
            "explain": (f"{who} but reached neither the Cerbo nor the internet. If the "
                        "router or Starlink runs off the inverter, turning the inverter "
                        f"off does this.{caveat}")}


def gaps(conn: sqlite3.Connection, now: int, days: int = 7) -> list[dict]:
    """Gaps in live battery data over the last `days` days, newest first; an
    ongoing gap has end None."""
    since = now - days * DAY
    ts = _battery_ts(conn, since - GAP_MIN_S)
    spans = [(x, y) for x, y in zip(ts, ts[1:]) if y - x > GAP_MIN_S and y > since]
    if ts and now - ts[-1] > GAP_MIN_S:
        spans.append((ts[-1], None))
    out = []
    for a, b in spans:
        g = {"start": a, "end": b, "duration_s": (b or now) - a}
        g.update(explain_gap(conn, a, b or now))
        out.append(g)
    return sorted(out, key=lambda g: -g["start"])


# Gaps with the same cause closer together than this read as one episode:
# a night of Cerbo WiFi dropouts is one problem, not nine.
EPISODE_JOIN_S = 3600


def episodes(gs: list[dict]) -> list[dict]:
    """Group gaps (newest first) into episodes of one cause."""
    out: list[dict] = []
    for g in sorted(gs, key=lambda g: g["start"]):
        last = out[-1] if out else None
        if (last and last["cause"] == g["cause"] and last["end"] is not None
                and g["start"] - last["end"] <= EPISODE_JOIN_S):
            last["end"] = g["end"]
            last["count"] += 1
            last["missing_s"] += g["duration_s"]
        else:
            out.append({**g, "count": 1, "missing_s": g["duration_s"]})
    return sorted(out, key=lambda e: -e["start"])


def health_view(conn: sqlite3.Connection, now: int, tz: tzinfo | None, days: int = 7) -> dict:
    gs = gaps(conn, now, days)
    return {
        "now": now, "days": days,
        "sources": sources(conn, now),
        "coverage": coverage(conn, now, tz, days),
        "episodes": episodes(gs),
        "missing_s": sum(g["duration_s"] for g in gs),
    }
