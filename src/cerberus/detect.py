"""Setup detection: settings proposed from what the Cerbo reports.

Two halves, split by process like the live snapshot (cerberus.live):

* The collector, which sees the Cerbo, writes an *inventory* to kv: every
  battery service the GX lists (name, instance, uc token, voltage, reported
  capacity), whether a GPS service exists, and the GX's time zone.
* The UI, which only reads the database, turns that inventory plus recent
  history into *proposals*: a value for a setting, why, and which settings
  must change with it. The owner applies one with the Settings screen's
  "Use" (the ordinary validated settings write); nothing is applied silently.

Only what the Cerbo can actually tell is proposed here: bank layout, bank
capacity, system voltage, time zone and position. Things that need the van
to run for a while (array Voc, engine charging current) are step 5.5.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics

from . import location as location_mod
from . import model as model_mod
from . import profile as profile_mod

KV_INVENTORY = "setup.inventory"
VERSION = 1
# The inventory is rewritten when what it lists changes, and at least this
# often so its voltages and timestamp stay current.
REFRESH_SECONDS = 300
VOLTAGE_DAYS = 7

# Battery services that measure the whole bank by themselves.
_MONITOR_NAME = re.compile(r"smartshunt|bmv|lynx|shunt", re.I)

# Settings that change together: a bank layout is one decision.
BANK_KEYS = ("bank.source", "bank.monitor_instance", "packs")


# --------------------------------------------------------------- collector side

def inventory(now: int, batteries_value: list, pack_health: dict,
              loc: location_mod.VanLocation) -> dict:
    """What the GX reports, from one system/0/Batteries message plus the
    cached per-instance Capacity and the location state."""
    bats = []
    for el in batteries_value or []:
        eid = str(el.get("id", ""))
        if not eid.startswith(model_mod.BATTERY_ID_PREFIX) or "soc" not in el:
            continue
        inst = el.get("instance")
        try:
            inst = int(inst)
        except (TypeError, ValueError):
            continue
        volts = el.get("voltage")
        bats.append({
            "id": eid,
            "instance": inst,
            "name": str(el.get("name") or ""),
            "uc": model_mod._uc_from_id(eid),
            "voltage": None if volts is None else round(float(volts), 2),
            "active": bool(el.get("active_battery_service")),
            "capacity_ah": (pack_health.get(inst) or {}).get("capacity_ah"),
        })
    bats.sort(key=lambda b: b["instance"])
    return {
        "v": VERSION,
        "ts": now,
        "batteries": bats,
        "gps": {"present": loc.gps_seen, "fix": bool(loc.fix)},
        "gx_timezone": loc.gx_timezone,
    }


def signature(inv: dict) -> str:
    """The inventory less what changes every message (voltages, the time)."""
    return json.dumps({
        "batteries": [{k: v for k, v in b.items() if k != "voltage"} for b in inv["batteries"]],
        "gps": inv["gps"], "tz": inv["gx_timezone"],
    }, sort_keys=True)


# ---------------------------------------------------------------------- UI side

def load_inventory(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_INVENTORY,)).fetchone()
    if not row:
        return None
    try:
        inv = json.loads(row[0])
    except ValueError:
        return None
    return inv if isinstance(inv, dict) and inv.get("v") == VERSION else None


def recent_voltage(conn: sqlite3.Connection, now: int, days: int = VOLTAGE_DAYS) -> float | None:
    """Median bank-side voltage over recent days. 0 V rows are a missing
    value (see LowChargeDetector), not a reading."""
    try:
        rows = conn.execute(
            "SELECT voltage FROM battery_samples WHERE ts >= ? AND voltage >= 5",
            (now - days * 86400,)).fetchall()
    except sqlite3.Error:
        return None
    return statistics.median(r[0] for r in rows) if rows else None


def last_position(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?",
                       (location_mod.KV_POSITION,)).fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0])
        return {"lat": float(p["lat"]), "lon": float(p["lon"]), "ts": p.get("ts")}
    except (ValueError, KeyError, TypeError):
        return None


def nominal_for(volts: float) -> int | None:
    """The system voltage a reading belongs to, if exactly one fits."""
    fits = [n for n in profile_mod.NOMINAL_VOLTAGES
            if profile_mod.profile(n).plausible(volts)]
    return fits[0] if len(fits) == 1 else None


def _p(value, reason, display=None, together=()):
    return {"value": value, "reason": reason,
            "display": display, "together": list(together)}


def _bank_layout(bats: list) -> tuple[dict, list[str]]:
    """Proposals for bank.source / monitor_instance / packs, and notes."""
    if not bats:
        return {}, ["The Cerbo doesn't list any battery yet, so the battery setup can't be detected."]
    monitors = [b for b in bats if _MONITOR_NAME.search(b["name"])]
    together = BANK_KEYS
    if len(bats) == 1:
        b = bats[0]
        what = b["name"] or f"battery service {b['instance']}"
        return {
            "bank.source": _p(model_mod.SOURCE_MONITOR,
                              f"The Cerbo lists one battery, {what}, so it measures the whole bank.",
                              together=together),
            "bank.monitor_instance": _p(None, "Only one battery: no need to pick one.",
                                        display="The Cerbo's system battery", together=together),
        }, []
    if len(monitors) == 1:
        m = monitors[0]
        return {
            "bank.source": _p(model_mod.SOURCE_MONITOR,
                              f"{m['name']} (instance {m['instance']}) measures the whole bank; "
                              f"the Cerbo also lists {len(bats) - 1} other battery service(s).",
                              together=together),
            "bank.monitor_instance": _p(m["instance"], f"{m['name']} is instance {m['instance']}.",
                                        together=together),
        }, []
    if not monitors and all(b["uc"] for b in bats):
        packs = {b["uc"]: i for i, b in enumerate(bats, start=1)}
        names = sorted({b["name"] for b in bats if b["name"]})
        kind = f" ({', '.join(names)})" if names else ""
        return {
            "bank.source": _p(model_mod.SOURCE_PACKS,
                              f"The Cerbo lists {len(bats)} battery packs{kind}, each with its own BMS; "
                              "the bank is their sum.", together=together),
            "bank.monitor_instance": _p(None, "Packs, not a monitor.", display="Not used",
                                        together=together),
            "packs": _p(packs, "Numbered in the Cerbo's instance order.",
                        display=pack_display(packs), together=together),
        }, []
    return {}, [f"The Cerbo lists {len(bats)} battery services and it isn't clear which one "
                "measures the bank; set the battery source by hand."]


def pack_display(packs: dict) -> str:
    if not packs:
        return "None"
    items = sorted(packs.items(), key=lambda kv: kv[1])
    return f"{len(items)} packs: " + ", ".join(f"{uc} = {pid}" for uc, pid in items)


def proposals(inv: dict | None, voltage: float | None = None,
              position: dict | None = None) -> tuple[dict, list[str]]:
    """{setting key: proposal} and notes on what could not be decided."""
    out: dict = {}
    notes: list[str] = []
    if inv is None:
        notes.append("Cerberus hasn't heard the Cerbo's equipment list yet; "
                     "detection starts once the collector is connected.")
        return out, notes
    bats = inv.get("batteries") or []

    layout, more = _bank_layout(bats)
    out.update(layout)
    notes += more

    # Capacity: the sum the batteries in the proposed layout report.
    source = (layout.get("bank.source") or {}).get("value")
    if source == model_mod.SOURCE_PACKS:
        counted = bats
    elif source == model_mod.SOURCE_MONITOR:
        inst = layout["bank.monitor_instance"]["value"]
        counted = [b for b in bats if b["instance"] == inst] if inst is not None else bats[:1]
    else:
        counted = []
    caps = [b.get("capacity_ah") for b in counted]
    if counted and all(c for c in caps):
        total = round(sum(caps))
        each = f" ({len(caps)} x {round(caps[0])} Ah)" if len(caps) > 1 and len(set(caps)) == 1 else ""
        out["bank.capacity_ah"] = _p(total, f"The batteries report {total} Ah{each}.")

    # System voltage: recent history if there is any, else the live reading.
    live = [b["voltage"] for b in bats if b.get("voltage") and b["voltage"] >= 5]
    if voltage is not None:
        basis, volts = f"over the last {VOLTAGE_DAYS} days", voltage
    elif live:
        basis, volts = "right now", statistics.median(live)
    else:
        basis, volts = None, None
    if volts is not None:
        n = nominal_for(volts)
        if n is not None:
            out["system.nominal_voltage"] = _p(n, f"The bank has read {volts:.1f} V {basis}.")
        else:
            notes.append(f"The bank reads {volts:.1f} V, which doesn't fit a 12, 24 or 48 V "
                         "lithium bank; check the system voltage by hand.")

    tz = inv.get("gx_timezone")
    if tz:
        out["system.timezone"] = _p(None, f"The Cerbo is set to {tz}. Following it keeps the "
                                          "van's clock right when you change it while travelling.",
                                    display=f"Follow the Cerbo ({tz})")

    gps = inv.get("gps") or {}
    if position is not None:
        lat, lon = round(position["lat"], 4), round(position["lon"], 4)
        why = "The last GPS fix."
        out["weather.latitude"] = _p(lat, why, together=("weather.longitude",))
        out["weather.longitude"] = _p(lon, why, together=("weather.latitude",))
    elif not gps.get("present"):
        notes.append("The Cerbo has no GPS, so set the home ZIP code (or latitude and longitude) by hand; "
                     "the weather forecast uses it.")
    return out, notes


def detect_view(conn: sqlite3.Connection, now: int) -> dict:
    """Everything the setup endpoints report: inventory, proposals, notes."""
    inv = load_inventory(conn)
    props, notes = proposals(inv, recent_voltage(conn, now), last_position(conn))
    return {"inventory": inv, "proposals": props, "notes": notes}
