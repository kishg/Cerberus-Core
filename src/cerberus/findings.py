"""Findings: what is wrong with the van's electrical system, in plain language.

Events are terse machine lines ("solar_path_report: path_mohm_at_absorption
92.4 >= 75.0 mOhm"). A finding is the owner-facing version: a title, what it
means, what to do, since when, and the evidence.

Derived on every read, from the database alone, and stored nowhere: a
finding's status is recomputed from history each time, so there is no state
to drift, and the UI (read-only) can call this directly. Two sources:

* **Measured** — the nightly solar_days rows and per-drive drives rows carry
  the actual numbers (path resistance, SoC at float, peak bank voltage), so
  these findings show a trend, not just an alert.
* **Edge events** — detectors raise ``X`` and later ``X_cleared``. Open while
  the last raise is newer than the last clear; an episode spans raise→clear.

The owner's notes (cerberus.notes) are the one input from outside the
database: passed in, attached to their findings, and used to say what changed
after them (apply_notes).

Wording rules: plain words, the van's own numbers, advice only (Cerberus never
commands equipment), and no claim the data cannot support.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import tzinfo

from .baseline import _median
from .location import day_start, local

URGENT, ATTENTION, INFO = "urgent", "attention", "info"
OPEN, RESOLVED = "open", "resolved"
_SEV_ORDER = {URGENT: 0, ATTENTION: 1, INFO: 2}

# A resolved finding stays visible this long, so "it was happening last week"
# is not lost the moment it clears.
RESOLVED_SHOW_DAYS = 14
DAY = 86400
# Same rule as LowChargeDetector: a pack voltage under this is a missing
# reading, not a flat pack (the 2026-10-29 17:24 false CRIT).
IMPLAUSIBLE_V = 5.0


@dataclass(frozen=True)
class Settings:
    tz: tzinfo | None = None
    path_limit_mohm: float = 75.0
    full_soc_pct: float = 98.0          # floating below this is an early float
    soc_trust_days: float = 7.0
    full_remind_days: float = 30.0
    def_low_pct: float = 20.0           # thresholds.def_low_pct
    service_due_mi: float = 500.0       # thresholds.service_due_mi


@dataclass
class Finding:
    key: str
    severity: str
    status: str
    title: str
    explain: str
    action: str
    first_seen: int | None = None
    last_seen: int | None = None
    # {"kind": "series", "label", "unit", "limit", "limit_label", "points": [[ts, v], ...]}
    # or {"kind": "events", "items": [[ts, text], ...]}
    evidence: dict | None = None
    extra: dict = field(default_factory=dict)
    # The owner's notes on this finding (cerberus.notes), oldest first.
    notes: list = field(default_factory=list)
    # The note marking it known, while that still holds (known_holds).
    known: dict | None = None

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "key", "severity", "status", "title", "explain", "action",
            "first_seen", "last_seen", "evidence", "notes", "known")}


# ---------------------------------------------------------------- helpers

def _day(ts: int, tz) -> str:
    d = local(ts, tz)
    return f"{d:%b} {d.day}"


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _json(s):
    try:
        v = json.loads(s) if s else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _detail(raw) -> dict:
    d = _json(raw)
    inner = d.get("detail", d)
    return inner if isinstance(inner, dict) else {}


def _kv(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _status_for(last_bad: int | None, still_bad: bool, now: int) -> str | None:
    """open / resolved / None (nothing worth showing)."""
    if still_bad:
        return OPEN
    if last_bad is not None and now - last_bad <= RESOLVED_SHOW_DAYS * DAY:
        return RESOLVED
    return None


# ---------------------------------------------------------------- measured

def _solar_days(conn, since_day: str) -> list[tuple[str, dict]]:
    return [(day, _json(m)) for day, m in conn.execute(
        "SELECT day, metrics FROM solar_days WHERE day >= ? ORDER BY day", (since_day,))]


def _day_ts(day: str, tz) -> int:
    return day_start(day, tz) + 12 * 3600          # plot at local noon


def solar_path(conn, s: Settings, now: int) -> list[Finding]:
    since = local(now - 30 * DAY, s.tz).date().isoformat()
    pts = []
    for day, m in _solar_days(conn, since):
        v = m.get("path_mohm_at_absorption", m.get("path_mohm"))
        if v is not None:
            pts.append((day, float(v)))
    if not pts:
        return []
    last_day, last_v = pts[-1]
    bad = [p for p in pts if p[1] >= s.path_limit_mohm]
    status = _status_for(_day_ts(bad[-1][0], s.tz) if bad else None,
                         last_v >= s.path_limit_mohm, now)
    if status is None:
        return []
    run_start = last_day
    for day, v in reversed(pts):                   # start of the current bad run
        if v < s.path_limit_mohm:
            break
        run_start = day
    low = min(v for _, v in pts)
    if status == OPEN:
        explain = (
            f"The wiring between the solar charge controller and the batteries measured "
            f"about {last_v:.0f} mΩ on {_day(_day_ts(last_day, s.tz), s.tz)}, above the "
            f"{s.path_limit_mohm:.0f} mΩ limit since {_day(_day_ts(run_start, s.tz), s.tz)}. "
            f"Resistance there makes the controller see a full battery too early, so it "
            f"cuts back charging while the bank is still filling. The lowest reading in "
            f"the last 30 days was {low:.0f} mΩ.")
    else:
        explain = (
            f"Resistance between the solar charge controller and the batteries was above "
            f"{s.path_limit_mohm:.0f} mΩ until {_day(_day_ts(bad[-1][0], s.tz), s.tz)}; "
            f"the latest reading is {last_v:.0f} mΩ.")
    return [Finding(
        key="solar_path_resistance", severity=ATTENTION, status=status,
        title="Solar charging is losing power at a connection",
        explain=explain,
        action=("Check and tighten the connections on the solar charge path: the controller's "
                "battery terminals, any breaker or fuse, bus bars and battery lugs. While solar "
                "is charging above 10 A, a joint that is warm or shows a voltage drop across it "
                "is the one to fix."),
        first_seen=_day_ts(run_start, s.tz) if status == OPEN else _day_ts(bad[0][0], s.tz),
        last_seen=_day_ts(last_day, s.tz),
        evidence={"kind": "series", "label": "Solar path resistance", "unit": " mΩ",
                  "limit": s.path_limit_mohm, "limit_label": "Limit",
                  "points": [[_day_ts(d, s.tz), round(v, 1)] for d, v in pts]},
    )]


def early_float(conn, s: Settings, now: int) -> list[Finding]:
    since = local(now - 14 * DAY, s.tz).date().isoformat()
    days = [(d, m["soc_at_float"]) for d, m in _solar_days(conn, since)
            if m.get("soc_at_float") is not None]
    if not days:
        return []
    early = [(d, v) for d, v in days if v < s.full_soc_pct]
    status = _status_for(_day_ts(early[-1][0], s.tz) if early else None,
                         days[-1][1] < s.full_soc_pct, now)
    if status is None:
        return []
    lo = min(v for _, v in early)
    explain = (
        f"On {len(early)} of the last {len(days)} days with solar charging, the charge "
        f"controller switched to float (maintenance) charging with the batteries at "
        f"{lo:.0f}%{'' if len(early) == 1 else f'–{max(v for _, v in early):.0f}%'} "
        f"instead of near full. ")
    explain += ("It is still happening." if status == OPEN else
                f"The last time was {_day(_day_ts(early[-1][0], s.tz), s.tz)}; since then it has "
                f"floated at {days[-1][1]:.0f}%.")
    return [Finding(
        key="early_float", severity=ATTENTION if status == OPEN else INFO, status=status,
        title="Solar stopped charging before the batteries were full",
        explain=explain,
        action=("Most often the same cause as connection resistance on the solar path. If that "
                "checks out, compare the charge controller's absorption settings with the "
                "battery maker's recommendation."),
        first_seen=_day_ts(early[0][0], s.tz), last_seen=_day_ts(early[-1][0], s.tz),
        evidence={"kind": "series", "label": "Battery % when float began", "unit": "%",
                  "limit": s.full_soc_pct, "limit_label": "Full",
                  "points": [[_day_ts(d, s.tz), v] for d, v in days]},
    )]


def soc_honesty(conn, s: Settings, now: int) -> list[Finding]:
    """The battery % is a running count that only resets at a true full charge."""
    out = []
    last_full = _kv(conn, "last_full_charge_ts")
    last_full = int(last_full) if last_full else None
    # A drive that put in more charge than the % admits to: reading high
    # until the next full charge.
    drift = None
    for ts, metrics, flags in conn.execute(
            "SELECT start_ts, metrics, flags FROM drives WHERE start_ts >= ? ORDER BY start_ts DESC",
            (now - 30 * DAY,)):
        fl = json.loads(flags or "[]")
        hit = next((f for f in fl if f.get("kind") == "soc_drift"), None)
        if hit:
            drift = (ts, hit, _json(metrics))
            break
    if drift and (last_full is None or last_full < drift[0]):
        ts, hit, m = drift
        out.append(Finding(
            key="soc_reads_high", severity=ATTENTION, status=OPEN,
            title="Battery % is reading high",
            explain=(f"On the {_day(ts, s.tz)} drive about {hit.get('bank_ah', m.get('bank_ah', 0)):.0f} Ah "
                     f"went into the batteries, but the battery % rose only "
                     f"{hit.get('soc_gain', 0):.0f} points. Until the next full charge it "
                     f"reads about {hit['value']:.0f} points higher than the true charge."),
            action=("Charge to full (a long drive, shore power, or a sunny day) so the battery "
                    "resets its count. Until then, plan as if there is less charge than shown."),
            first_seen=ts, last_seen=ts))
    elif drift:
        out.append(Finding(
            key="soc_reads_high", severity=INFO, status=RESOLVED,
            title="Battery % is reading high",
            explain=(f"After the {_day(drift[0], s.tz)} drive the battery % read about "
                     f"{drift[1]['value']:.0f} points high; the full charge on "
                     f"{_day(last_full, s.tz)} reset it."),
            action="Nothing to do now.", first_seen=drift[0], last_seen=last_full))
    if last_full is None or now - last_full > s.full_remind_days * DAY:
        days = None if last_full is None else (now - last_full) / DAY
        out.append(Finding(
            key="no_full_charge", severity=ATTENTION, status=OPEN,
            title="No full charge in a long time",
            explain=("There is no true full charge on record, " if days is None else
                     f"The batteries last reached a true full charge {days:.0f} days ago, ")
                    + "so the battery % has had nothing to correct it and may be off by "
                      "10 points or more.",
            action=("Charge to full soon: a long drive, a day on shore power, or a full sunny "
                    "day with light loads."),
            # A state, not an occurrence: last_seen is just "now".
            first_seen=last_full, last_seen=now, extra={"ongoing": True}))
    return out


def _drive_flags(conn, metric: str, now: int, days: int = 30):
    rows = conn.execute(
        "SELECT start_ts, flags FROM drives WHERE start_ts >= ? ORDER BY start_ts",
        (now - days * DAY,)).fetchall()
    hits = []
    for ts, flags in rows:
        for f in json.loads(flags or "[]"):
            if f.get("kind") == "limit" and f.get("metric") == metric:
                hits.append((ts, f))
    return rows, hits


def alternator_overshoot(conn, s: Settings, now: int) -> list[Finding]:
    rows, hits = _drive_flags(conn, "peak_bank_v", now)
    if not hits:
        return []
    recent = [ts for ts, _ in rows][-5:]
    still = hits[-1][0] in recent                 # flagged within the last 5 drives
    status = _status_for(hits[-1][0], still, now)
    if status is None:
        return []
    ts, f = hits[-1]
    return [Finding(
        key="alternator_overshoot", severity=ATTENTION, status=status,
        title="Driving pushed the batteries above their charging limit",
        explain=((f"On {_day(ts, s.tz)} the bank reached {f['value']:.2f} V while driving, over "
                  f"its {f['limit'].lstrip('>= ')} limit. " if len(hits) == 1 else
                  f"On {len(hits)} drives in the last 30 days the bank went over its "
                  f"{f['limit'].lstrip('>= ')} limit, most recently {f['value']:.2f} V on "
                  f"{_day(ts, s.tz)}. ")
                 + "Lithium cells charged past their limit wear faster and can trip the "
                   "battery's own high-voltage protection."),
        action=("If the alternator charges the house batteries directly, a DC-DC charger "
                "between them regulates this. Until then, avoid long drives with the "
                "batteries already full."),
        first_seen=hits[0][0], last_seen=ts,
        evidence={"kind": "events", "items": [
            [t, f"{h['value']:.2f} V peak"] for t, h in hits[-10:]]},
    )]


def joint_heating(conn, s: Settings, now: int) -> list[Finding]:
    rows, hits = _drive_flags(conn, "joint_peak_c", now)
    if not hits:
        return []
    still = hits[-1][0] in [ts for ts, _ in rows][-5:]
    status = _status_for(hits[-1][0], still, now)
    if status is None:
        return []
    ts, f = hits[-1]
    return [Finding(
        key="joint_heating", severity=URGENT if status == OPEN else INFO, status=status,
        title="A connection on the alternator charge path is running hot",
        explain=(f"The temperature probe on that connection reached {f['value']:.0f} °C on the "
                 f"{_day(ts, s.tz)} drive, hotter than anything on record. Heat at a joint means "
                 f"resistance, and it gets worse as the joint loosens."),
        action=("With the engine off and the house batteries disconnected, check and tighten "
                "that connection, and look for discoloured insulation."),
        first_seen=hits[0][0], last_seen=ts)]


def pack_imbalance(conn, s: Settings, now: int) -> list[Finding]:
    rows = conn.execute(
        "SELECT ts, detail FROM events WHERE type = 'current_share_imbalance' AND ts >= ? "
        "ORDER BY ts", (now - 7 * DAY,)).fetchall()
    if not rows:
        return []
    ts, raw = rows[-1]
    d = _detail(raw)
    status = OPEN if now - ts <= 3 * DAY else RESOLVED
    return [Finding(
        key="pack_imbalance", severity=INFO, status=status,
        title="One battery is carrying more of the current",
        explain=(f"Pack {d.get('pack')} carried {d.get('share_pct', 0):.0f}% of "
                 f"{d.get('current', 0):.0f} A on {_day(ts, s.tz)}, where an even share is "
                 f"{d.get('expected_pct', 0):.0f}%; {_times(len(rows))} in the last 7 days. At low "
                 f"currents this can also be the battery's current-sensor resolution."),
        action=("Compare the cables and connections on each pack: equal lengths and tight "
                "lugs. The pack with the shortest or best path takes more current."),
        first_seen=rows[0][0], last_seen=ts)]


# ---------------------------------------------------------------- engine (OBD)


def engine_faults(conn, s: Settings, now: int) -> list[Finding]:
    """Trouble codes the engine reported, read once per engine-on session
    (obd_dtcs). Open while the latest stored-codes read (service 03) has any."""
    rows = conn.execute(
        "SELECT ts, service, codes FROM obd_dtcs ORDER BY ts").fetchall()
    stored = [(ts, c) for ts, svc, c in rows if svc == "03" and c is not None]
    if not stored:
        return []
    bad = [(ts, c) for ts, c in stored if c]
    last_ts, last_codes = stored[-1]
    status = _status_for(bad[-1][0] if bad else None, bool(last_codes), now)
    if status is None:
        return []
    ts, codes = bad[-1]
    pending = [c for t, svc, c in rows if svc == "07" and t == last_ts and c]
    explain = (f"The engine reported trouble code{'s' if ',' in codes else ''} "
               f"{codes.replace(',', ', ')} on {_day(ts, s.tz)}.")
    if pending:
        explain += f" Pending (not yet confirmed): {pending[0].replace(',', ', ')}."
    if status == RESOLVED:
        explain += f" None stored at the read on {_day(last_ts, s.tz)}."
    return [Finding(
        key="engine_faults", severity=ATTENTION if status == OPEN else INFO, status=status,
        title="The engine reported trouble codes",
        explain=explain,
        action=("Look the codes up for this engine, or have a scan tool or the dealer read "
                "them in full. Cerberus only reads codes; it never clears them."),
        first_seen=bad[0][0], last_seen=ts,
        evidence={"kind": "events", "items": [[t, f"Stored: {c.replace(',', ', ')}"] for t, c in bad[-10:]]})]


def def_low(conn, s: Settings, now: int) -> list[Finding]:
    """Diesel exhaust fluid (AdBlue) under s.def_low_pct, worth a refill
    before a trip: an empty tank puts a modern diesel into a speed-limited
    mode. Reads the OBD tank level and the imported one (cerberus.vehicle),
    whichever is newer, since the import also covers parked time."""
    rows = sorted(conn.execute(
        "SELECT ts, value FROM obd_samples WHERE name = 'def_level_pct' AND value IS NOT NULL "
        "UNION ALL SELECT ts, value FROM vehicle_samples "
        "WHERE name = 'def_level_pct' AND value IS NOT NULL").fetchall())
    if not rows:
        return []
    limit = s.def_low_pct
    low = [(ts, v) for ts, v in rows if v < limit]
    ts, v = rows[-1]
    status = _status_for(low[-1][0] if low else None, v < limit, now)
    if status is None:
        return []
    lts, lv = low[-1]
    explain = (f"The DEF (AdBlue) tank read {lv:.0f}% on {_day(lts, s.tz)}, under the "
               f"{limit:.0f}% alert level. When it runs empty the engine limits the van's speed until it is refilled.")
    if status == RESOLVED:
        explain += f" It read {v:.0f}% on {_day(ts, s.tz)}."
    return [Finding(
        key="def_low", severity=ATTENTION if status == OPEN else INFO, status=status,
        title="DEF (AdBlue) is running low",
        explain=explain,
        action="Top up the DEF tank before a long drive." if status == OPEN else "Nothing to do now.",
        first_seen=low[0][0], last_seen=lts)]


# ---------------------------------------------------------------- vehicle import

# starter_battery_state (cerberus.vehicle): Mercedes coding.
_STARTER = {0: None, 4: None, 1: (INFO, "fair"), 3: (ATTENTION, "weak"), 2: (URGENT, "poor")}
# A tyre this far below its own 30-day median is losing air.
TYRE_DROP_PCT = 10.0
_TYRES = {"tire_fl_psi": "front left", "tire_fr_psi": "front right",
          "tire_rl_psi": "rear left", "tire_rr_psi": "rear right"}


def _vehicle_series(conn, name: str, since: int = 0) -> list[tuple[int, float]]:
    return conn.execute(
        "SELECT ts, value FROM vehicle_samples WHERE name = ? AND ts >= ? AND value IS NOT NULL "
        "ORDER BY ts", (name, since)).fetchall()


def starter_battery(conn, s: Settings, now: int) -> list[Finding]:
    """The chassis's own verdict on its starter battery (imported)."""
    rows = _vehicle_series(conn, "starter_battery_state")
    if not rows:
        return []
    bad = [(ts, int(v)) for ts, v in rows if _STARTER.get(int(v))]
    ts, v = rows[-1]
    status = _status_for(bad[-1][0] if bad else None, bool(_STARTER.get(int(v))), now)
    if status is None:
        return []
    bts, bv = bad[-1]
    sev, word = _STARTER[bv]
    explain = (f"The van's own battery check rated the starter battery {word} on "
               f"{_day(bts, s.tz)}. This is the chassis battery the engine starts from, "
               f"not the house bank.")
    if status == RESOLVED:
        explain += f" It rated it good again on {_day(ts, s.tz)}."
    return [Finding(
        key="starter_battery", severity=sev if status == OPEN else INFO, status=status,
        title="The starter battery is getting weak",
        explain=explain,
        action=("Have the starter battery tested. Long parked spells with loads on the "
                "chassis side drain it." if status == OPEN else "Nothing to do now."),
        first_seen=bad[0][0], last_seen=bts)]


def low_tyres(conn, now: int) -> dict[str, tuple[int, float, float]]:
    """Tyres clearly below their own 30-day level: name -> (ts, psi, median)."""
    low = {}
    for name in _TYRES:
        rows = _vehicle_series(conn, name, now - 30 * DAY)
        if len(rows) < 5:
            continue
        med = _median([v for _, v in rows[:-1]])
        ts, v = rows[-1]
        if med and v <= med * (1 - TYRE_DROP_PCT / 100):
            low[name] = (ts, v, med)
    return low


def tyre_pressure(conn, s: Settings, now: int) -> list[Finding]:
    """The van's tyre warning, or a tyre clearly below its own recent level."""
    problems, last_bad, first_bad = [], None, None
    warn = _vehicle_series(conn, "tire_warning")
    if warn:
        hits = [ts for ts, v in warn if v >= 1]
        if hits:
            last_bad, first_bad = hits[-1], hits[0]
        if warn[-1][1] >= 1:
            problems.append("the van's tyre-pressure warning is on")
    for name, (ts, v, med) in low_tyres(conn, now).items():
        problems.append(f"{_TYRES[name]} is at {v:.0f} psi, down from {med:.0f} psi")
        last_bad = max(last_bad or 0, ts)
        first_bad = min(first_bad or ts, ts)
    status = _status_for(last_bad, bool(problems), now)
    if status is None:
        return []
    return [Finding(
        key="tyre_pressure", severity=ATTENTION if status == OPEN else INFO, status=status,
        title="A tyre is low on pressure",
        explain=("Tyre pressure: " + "; ".join(problems) + "." if problems else
                 f"The tyre warning was on until {_day(last_bad, s.tz)}; it has cleared."),
        action=("Check the pressures cold and top up to the door-pillar figure; look for "
                "a nail or a slow leak." if status == OPEN else "Nothing to do now."),
        first_seen=first_bad, last_seen=last_bad)]


def service_due(conn, s: Settings, now: int) -> list[Finding]:
    """The chassis's own service countdown (imported), once it is under
    s.service_due_mi. Resolves when a service resets it."""
    rows = _vehicle_series(conn, "service_due_mi")
    if not rows:
        return []
    limit = s.service_due_mi
    due = [(ts, v) for ts, v in rows if v < limit]
    ts, v = rows[-1]
    status = _status_for(due[-1][0] if due else None, v < limit, now)
    if status is None:
        return []
    dts, dv = due[-1]
    if status == RESOLVED:
        explain = (f"The van's next service was {dv:.0f} mi away on {_day(dts, s.tz)}; "
                   f"it now reads {v:.0f} mi, so the countdown has been reset.")
    elif v <= 0:
        explain = f"The van reports its service is due now ({_day(ts, s.tz)})."
    else:
        explain = (f"The van's next service is {v:.0f} mi away ({_day(ts, s.tz)}), under "
                   f"the {limit:.0f} mi reminder level.")
    return [Finding(
        key="service_due", severity=ATTENTION if status == OPEN and v <= 0 else INFO,
        status=status,
        title="A service is coming up",
        explain=explain,
        action="Book the service." if status == OPEN else "Nothing to do now.",
        first_seen=due[0][0], last_seen=dts)]


def engine_light(conn, s: Settings, now: int) -> list[Finding]:
    """The engine warning light (MIL) on, from OBD (mil_on, read while the
    engine runs) or the import (engine_light). This van's import has never
    sent the light, perhaps only once it lights up; OBD reads it every drive."""
    rows = sorted(conn.execute(
        "SELECT ts, value FROM obd_samples WHERE name = 'mil_on' AND value IS NOT NULL "
        "UNION ALL SELECT ts, value FROM vehicle_samples "
        "WHERE name = 'engine_light' AND value IS NOT NULL").fetchall())
    hits = [ts for ts, v in rows if v >= 1]
    if not hits:
        return []
    on = rows[-1][1] >= 1
    status = _status_for(hits[-1], on, now)
    if status is None:
        return []
    return [Finding(
        key="engine_light", severity=ATTENTION if status == OPEN else INFO, status=status,
        title="The van reports its engine warning light",
        explain=(f"The engine warning light was on, on {_day(hits[-1], s.tz)}."
                 if on else f"The engine warning light was on until {_day(hits[-1], s.tz)}; it has cleared."),
        action=("Read the trouble codes: Cerberus reads them while the engine runs (with an OBD "
                "adapter), or have a scan tool or the dealer read them." if status == OPEN
                else "Nothing to do now."),
        first_seen=hits[0], last_seen=hits[-1])]


def vehicle_fluids(conn, s: Settings, now: int) -> list[Finding]:
    """Coolant, brake fluid and washer warnings from the chassis."""
    names = {"coolant_low": ("coolant", ATTENTION), "brake_fluid_low": ("brake fluid", ATTENTION),
             "washer_low": ("washer fluid", INFO)}
    open_, last_bad, first_bad, sev = [], None, None, INFO
    for name, (label, level) in names.items():
        rows = _vehicle_series(conn, name)
        hits = [ts for ts, v in rows if v >= 1]
        if not hits:
            continue
        last_bad = max(last_bad or 0, hits[-1])
        first_bad = min(first_bad or hits[0], hits[0])
        if rows[-1][1] >= 1:
            open_.append(label)
            if level == ATTENTION:
                sev = ATTENTION
    status = _status_for(last_bad, bool(open_), now)
    if status is None:
        return []
    return [Finding(
        key="vehicle_fluids", severity=sev if status == OPEN else INFO, status=status,
        title="The van reports a low fluid level",
        explain=(f"Low {', '.join(open_)}, reported by the van." if open_ else
                 f"A low-fluid warning was on until {_day(last_bad, s.tz)}; it has cleared."),
        action=("Top up as the owner's manual says. Low coolant or brake fluid can mean a "
                "leak: have it checked if it drops again." if status == OPEN else "Nothing to do now."),
        first_seen=first_bad, last_seen=last_bad)]


# ---------------------------------------------------------------- edge events

def _episodes(rows, ignore=None):
    """Fold (ts, is_raise, detail) rows into episodes per stream. Returns
    (open_since, last_raise_ts, last_clear_ts, count, last_detail) or None."""
    open_since = last_raise = last_clear = None
    count, last_detail, first = 0, {}, None
    for ts, is_raise, det in rows:
        if is_raise:
            if ignore and ignore(det):
                continue
            count += 1
            last_raise, last_detail = ts, det
            if open_since is None:
                open_since = ts
            first = first or ts
        elif open_since is not None:
            open_since, last_clear = None, ts
    if last_raise is None:
        return None
    return {"open_since": open_since, "last_raise": last_raise, "last_clear": last_clear,
            "count": count, "detail": last_detail, "first": first}


@dataclass(frozen=True)
class _Edge:
    key: str
    types: tuple[str, ...]          # raise types; clear is type + "_cleared"
    severity: str
    title: str
    explain: object                 # callable(detail, episode, settings) -> str
    action: str
    ignore: object = None


def _edge_rows(conn, types) -> list:
    qs = ",".join("?" * (2 * len(types)))
    names = [t for t in types] + [t + "_cleared" for t in types]
    return [(ts, not typ.endswith("_cleared"), _detail(raw)) for ts, typ, raw in conn.execute(
        f"SELECT ts, type, detail FROM events WHERE type IN ({qs}) ORDER BY ts", names)]


EDGES = (
    _Edge("solar_output_open", ("solar_output_open",), URGENT,
          "Solar isn't reaching the batteries",
          lambda d, e, s: (f"The panels have sun (PV at {d.get('pv_voltage', 0):.0f} V) but no "
                           f"power reaches the batteries, and the charge controller reports no "
                           f"fault: a tripped breaker or open connection between the controller "
                           f"and the batteries. It has happened {_times(e['count'])}."),
          "Check the breaker or fuse between the charge controller and the batteries and reset "
          "it. One that trips again on sunny mornings may be undersized or worn."),
    _Edge("cell_imbalance", ("cell_imbalance", "cell_imbalance_bank"), ATTENTION,
          "Battery cells are out of balance",
          lambda d, e, s: (f"Cells differed by {d.get('spread_mv', 0)} mV"
                           + (f" ({d['cell_min_mv']}–{d['cell_max_mv']} mV)" if "cell_min_mv" in d else "")
                           + (f" in pack {d['pack']}" if "pack" in d else "")
                           + ". Healthy cells stay within about 30 mV of each other."),
          "This usually corrects itself over a few full charges, when the battery balances its "
          "cells. If it keeps coming back, contact the battery maker."),
    _Edge("pack_soc_spread", ("soc_drift",), ATTENTION,
          "The batteries are at different charge levels",
          lambda d, e, s: (f"Pack charge levels were {d.get('spread_pct', 0):.0f} points apart. "
                           f"Packs in one bank should stay within a few points of each other."),
          "Usually evens out after a full charge. If one pack keeps lagging, check that its "
          "cables and connections match the others."),
    _Edge("battery_hot", ("temp_high",), URGENT, "A battery is too hot",
          lambda d, e, s: f"Pack {d.get('pack')} reached {d.get('temperature', 0):.0f} °C.",
          "Reduce charging and heavy loads, and improve airflow around the batteries."),
    _Edge("battery_cold", ("temp_low",), ATTENTION, "A battery is too cold to charge",
          lambda d, e, s: (f"Pack {d.get('pack')} is at {d.get('temperature', 0):.0f} °C. Lithium "
                           f"batteries must not charge near freezing; most block charging "
                           f"themselves."),
          "Warm the battery compartment before relying on charging."),
    _Edge("low_charge", ("low_soc",), ATTENTION, "The batteries are low",
          lambda d, e, s: f"The lowest pack is at {d.get('min_soc', 0):.0f}%.",
          "Cut loads or charge: drive, plug in, or wait for sun."),
    _Edge("critical_voltage", ("low_soc_crit",), URGENT, "Battery voltage is critically low",
          lambda d, e, s: (f"A pack reached {d.get('min_voltage', 0):.2f} V. Close to this, the "
                           f"battery disconnects itself to protect the cells."),
          "Turn off loads now and charge as soon as possible.",
          ignore=lambda d: (d.get("min_voltage") is not None and d["min_voltage"] < IMPLAUSIBLE_V)),
)

_ALARM = re.compile(r"^alarm:(?P<svc>[a-z]+)/(?P<inst>\d+)/(?:L\d/)?(?P<name>\w+?)(?P<clear>_cleared)?$")


def edge_findings(conn, s: Settings, now: int) -> list[Finding]:
    out = []
    for e in EDGES:
        ep = _episodes(_edge_rows(conn, e.types), e.ignore)
        if not ep:
            continue
        status = _status_for(ep["last_clear"] or ep["last_raise"], ep["open_since"] is not None, now)
        if status is None:
            continue
        text = e.explain(ep["detail"], ep, s)
        if status == RESOLVED:
            text += f" Cleared {_day(ep['last_clear'], s.tz)}."
        out.append(Finding(
            key=e.key, severity=e.severity if status == OPEN else INFO, status=status,
            title=e.title, explain=text, action=e.action if status == OPEN else "Nothing to do now.",
            first_seen=ep["open_since"] or ep["first"], last_seen=ep["last_raise"]))
    return out + alarm_findings(conn, s, now)


def alarm_findings(conn, s: Settings, now: int) -> list[Finding]:
    """Alarms the Victron devices raise themselves, grouped by kind."""
    streams: dict[tuple[str, str], list] = {}
    for ts, typ, raw in conn.execute(
            "SELECT ts, type, detail FROM events WHERE type LIKE 'alarm:%' ORDER BY ts"):
        m = _ALARM.match(typ)
        if not m:
            continue
        streams.setdefault((m["svc"], m["name"]), []).append((ts, not m["clear"], {"inst": m["inst"]}))
    out = []
    for (svc, name), rows in streams.items():
        ep = _episodes(rows)
        status = _status_for(ep["last_clear"] or ep["last_raise"], ep["open_since"] is not None, now)
        if status is None:
            continue
        # Distinct times: the inverter reports each alarm twice (total and L1).
        recent_ts = sorted({ts for ts, r, _ in rows if r and ts >= now - 30 * DAY})
        recent = len(recent_ts)
        # The span the explanation talks about: open since its raise, else the
        # first alarm in the 30-day window it counts.
        first = ep["open_since"] or (recent_ts[0] if recent_ts else ep["last_raise"])
        when = _day(ep["last_raise"], s.tz)
        if svc == "vebus" and name == "LowBattery":
            f = Finding(
                key="inverter_low_battery", severity=ATTENTION, status=status,
                title="The inverter saw low battery voltage",
                explain=(f"The inverter raised its low-battery alarm {_times(recent)} in the last "
                         f"30 days, most recently {when}. When the batteries themselves are well "
                         f"charged, this happens under heavy AC loads: the voltage at the inverter "
                         f"dips because of resistance in the cables between them."),
                action=("Check the cables and connections between the batteries and the "
                        "inverter: tight lugs, clean contacts, and cable sized for the inverter."),
                first_seen=first, last_seen=ep["last_raise"])
        elif svc == "battery" and name == "HighVoltage":
            f = Finding(
                key="battery_high_voltage", severity=URGENT, status=status,
                title="A battery reported high voltage",
                explain=(f"The batteries raised a high-voltage alarm {_times(recent)} in the last "
                         f"30 days, most recently {when}. That means they were charged harder "
                         f"than they asked for, usually by the alternator into a full bank."),
                action=("If the alternator charges the house batteries directly, a DC-DC "
                        "charger between them regulates it."),
                first_seen=first, last_seen=ep["last_raise"])
        else:
            f = Finding(
                key=f"alarm_{svc}_{name}", severity=ATTENTION, status=status,
                title=f"{svc.capitalize()} alarm: {re.sub(r'(?<!^)(?=[A-Z])', ' ', name).lower()}",
                explain=f"Raised {_times(recent)} in the last 30 days, most recently {when}.",
                action="Check the device's own app or display for details.",
                first_seen=first, last_seen=ep["last_raise"])
        if status == RESOLVED:
            f.severity, f.action = INFO, "Nothing to do now."
            f.explain += f" Cleared {_day(ep['last_clear'], s.tz)}."
        out.append(f)
    return out


# ---------------------------------------------------------------- notes

def _mean(vals):
    return sum(vals) / len(vals)


def note_context(f: Finding, note: dict, s: Settings) -> str | None:
    """What changed after the owner's note: did the fix they wrote down work?
    Only what the data shows; None when it shows nothing yet."""
    when = _day(note["ts"], s.tz)
    ev = f.evidence or {}
    if ev.get("kind") == "series":
        before = [v for t, v in ev["points"] if t <= note["ts"] and v is not None]
        after = [v for t, v in ev["points"] if t > note["ts"] and v is not None]
        if not before or not after:
            return None
        unit = ev.get("unit", "")
        n = "reading" if len(after) == 1 else f"{len(after)} readings"
        return (f"Since your note on {when}: {ev['label'].lower()} averaged "
                f"{_mean(after):.0f}{unit} over {n}, against {_mean(before):.0f}{unit} before.")
    if f.extra.get("ongoing"):
        return None
    if f.last_seen is not None and f.last_seen > note["ts"]:
        return f"It has come up again since your note on {when}."
    if f.status == RESOLVED:
        return f"It has not come back since your note on {when}."
    return None


def known_holds(f: Finding, note: dict) -> bool:
    """A finding marked known stays known until it happens again after the
    note: a new occurrence for an event finding, a new episode (bad run,
    missed full charge) for a measured or ongoing one. So a known problem
    that comes back, or clears and returns, is flagged again by itself."""
    if (f.evidence or {}).get("kind") == "series" or f.extra.get("ongoing"):
        return f.first_seen is None or f.first_seen <= note["ts"]
    return f.last_seen is None or f.last_seen <= note["ts"]


def apply_notes(findings: list[Finding], notes: list[dict], s: Settings) -> list[Finding]:
    """Attach the owner's notes (cerberus.notes) to their findings, say what
    changed since the latest one, and mark them known where a note says so."""
    by_key: dict[str, list[dict]] = {}
    for n in notes:
        by_key.setdefault(n["key"], []).append(n)
    for f in findings:
        f.notes = sorted(by_key.get(f.key, []), key=lambda n: n["ts"])
        if f.notes:
            line = note_context(f, f.notes[-1], s)
            if line:
                f.explain += " " + line
        mark = next((n for n in reversed(f.notes) if n.get("known")), None)
        if mark and f.status == OPEN:
            if known_holds(f, mark):
                f.known = {"id": mark["id"], "ts": mark["ts"]}
            else:
                f.explain += (f" You marked this known on {_day(mark['ts'], s.tz)}; it has "
                              f"happened again since, so it is flagged again.")
    return findings


# ---------------------------------------------------------------- all

DERIVERS = (solar_path, early_float, soc_honesty, alternator_overshoot, joint_heating,
            pack_imbalance, engine_faults, def_low, starter_battery, tyre_pressure,
            service_due, engine_light, vehicle_fluids, edge_findings)


def derive(conn: sqlite3.Connection, s: Settings, now: int,
           notes: list[dict] | None = None) -> list[Finding]:
    """Every current finding: open ones by severity (those marked known
    after the rest), then recently resolved, each newest first. ``notes``
    are the owner's (cerberus.notes)."""
    out: list[Finding] = []
    for fn in DERIVERS:
        out.extend(fn(conn, s, now))
    apply_notes(out, notes or [], s)
    return sorted(out, key=lambda f: (f.status != OPEN, f.known is not None, _SEV_ORDER[f.severity],
                                      -(f.last_seen or 0)))
