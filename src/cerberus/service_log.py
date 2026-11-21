"""The van's service log: the maintenance the owner did, and what the chassis
reported along the way.

Two kinds of entry, shown as one timeline on the Chassis tab:

* **Service items** the owner writes: an oil change, new tyres, brake pads, a
  repair. A day, what was done, and optionally the odometer, the shop and the
  cost. Written by the UI, so they live in a small file beside config.yaml,
  not in the database (the UI never writes the database); like the notes file
  it is per-box state, git-ignored, and carried over by cerberus.migrate.
* **Chassis events** worked out from data the collector already keeps, never
  stored twice: a trouble code first stored or pending, a code no longer
  stored (cleared by a scan tool or the dealer, or by the engine itself after
  enough clean drive cycles: Cerberus only reads codes, it never clears them),
  the engine warning light coming on and going off, and the van's own service
  countdown being reset.

Each entry carries the odometer near its time when the van reported one, so a
log of "oil at 48,210 mi" needs no typing beyond the work itself.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, time as dtime, tzinfo
from pathlib import Path

SERVICE_FILE = ".cerberus-service-log.json"
CATEGORIES = {
    "oil": "Oil and filter", "filters": "Filters", "tyres": "Tyres", "brakes": "Brakes",
    "fluids": "Fluids", "def": "DEF (AdBlue)", "battery": "Chassis battery",
    "inspection": "Inspection", "repair": "Repair", "other": "Other",
}
MAX_TEXT = 1000
MAX_SHOP = 120
MAX_ENTRIES = 1000
MAX_ODOMETER_MI = 2_000_000
MAX_COST = 1_000_000
# How far from an event an odometer reading may be and still be "the odometer then".
ODOMETER_NEAR_S = 2 * 86400
# A countdown that jumps up by more than this between readings was reset by a service.
RESET_JUMP_MI = 1000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")

# The UI server is threaded: one write at a time, so two phones saving at once
# can't drop each other's entry.
_lock = threading.Lock()


class ServiceError(ValueError):
    pass


def path_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(SERVICE_FILE)


def _num(v):
    return None if v is None else float(v)


def read(config_path: str | Path) -> list[dict]:
    """Every owner entry, oldest day first. A missing or damaged file is none."""
    try:
        raw = json.loads(path_for(config_path).read_text())
    except (FileNotFoundError, ValueError):
        return []
    out = []
    for e in raw.get("entries", []) if isinstance(raw, dict) else []:
        try:
            if not _DATE.match(str(e["date"])):
                continue
            out.append({"id": str(e["id"]), "date": str(e["date"]),
                        "category": e["category"] if e.get("category") in CATEGORIES else "other",
                        "text": str(e["text"]), "shop": str(e.get("shop") or ""),
                        "odometer_mi": _num(e.get("odometer_mi")), "cost": _num(e.get("cost")),
                        "created_ts": int(e.get("created_ts") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda e: (e["date"], e["created_ts"]))


def _write(config_path: str | Path, entries: list[dict]) -> None:
    p = path_for(config_path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".service-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"entries": entries}, f, indent=1)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _bounded(v, name: str, hi: float):
    if v is None or v == "":
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ServiceError(f"the {name} is a number") from None
    if not 0 <= v <= hi:
        raise ServiceError(f"the {name} looks wrong")
    return v


def _validate(fields: dict, today: date) -> dict:
    day = str(fields.get("date") or "").strip()
    try:
        if not _DATE.match(day):
            raise ValueError
        d = date.fromisoformat(day)
    except ValueError:
        raise ServiceError("pick the day the work was done") from None
    if d.year < 1990 or d > today:
        raise ServiceError("the day can't be in the future")
    category = str(fields.get("category") or "other")
    if category not in CATEGORIES:
        raise ServiceError("pick what kind of work it was")
    text = str(fields.get("text") or "").strip()
    if not text:
        raise ServiceError("say what was done")
    if len(text) > MAX_TEXT:
        raise ServiceError(f"keep it under {MAX_TEXT} characters")
    shop = str(fields.get("shop") or "").strip()
    if len(shop) > MAX_SHOP:
        raise ServiceError(f"keep the shop under {MAX_SHOP} characters")
    return {"date": day, "category": category, "text": text, "shop": shop,
            "odometer_mi": _bounded(fields.get("odometer_mi"), "odometer", MAX_ODOMETER_MI),
            "cost": _bounded(fields.get("cost"), "cost", MAX_COST)}


def add(config_path: str | Path, fields: dict, now: int | None = None) -> dict:
    """Add a service item: date (YYYY-MM-DD), category, text, and optionally
    shop, odometer_mi and cost."""
    now = int(time.time()) if now is None else int(now)
    entry = {"id": secrets.token_hex(6), **_validate(fields, date.fromtimestamp(now + 86400)),
             "created_ts": now}
    with _lock:
        entries = read(config_path)
        if len(entries) >= MAX_ENTRIES:
            raise ServiceError(f"there are already {MAX_ENTRIES} entries; delete some first")
        _write(config_path, entries + [entry])
    return entry


def update(config_path: str | Path, entry_id, fields: dict, now: int | None = None) -> dict | None:
    """Replace an entry's fields (a typo in the mileage), keeping its id."""
    now = int(time.time()) if now is None else int(now)
    clean = _validate(fields, date.fromtimestamp(now + 86400))
    with _lock:
        entries = read(config_path)
        hit = next((e for e in entries if e["id"] == str(entry_id)), None)
        if hit is None:
            return None
        hit.update(clean)
        _write(config_path, entries)
    return hit


def delete(config_path: str | Path, entry_id) -> bool:
    with _lock:
        entries = read(config_path)
        kept = [e for e in entries if e["id"] != str(entry_id)]
        if len(kept) == len(entries):
            return False
        _write(config_path, kept)
    return True


# ---------------------------------------------------------------- chassis events


def odometer_at(conn: sqlite3.Connection, ts: int) -> float | None:
    """The van's odometer closest to ts (OBD or an import), if one is near."""
    best = None
    for table in ("obd_samples", "vehicle_samples"):
        for op, order in (("<=", "DESC"), (">=", "ASC")):
            row = conn.execute(
                f"SELECT ts, value FROM {table} WHERE name = 'odometer_mi' AND value IS NOT NULL "
                f"AND ts {op} ? ORDER BY ts {order} LIMIT 1", (ts,)).fetchone()
            if row and abs(row[0] - ts) <= ODOMETER_NEAR_S and (
                    best is None or abs(row[0] - ts) < abs(best[0] - ts)):
                best = row
    return None if best is None else round(best[1])


def _codes(s: str) -> set[str]:
    return {c for c in s.split(",") if c}


def dtc_events(conn: sqlite3.Connection) -> list[dict]:
    """Codes appearing in and leaving the stored (03) reads, and codes first
    seen pending (07). A failed read (codes NULL) is no evidence either way."""
    out = []
    prev: dict[str, set[str]] = {}
    for ts, service, codes in conn.execute(
            "SELECT ts, service, codes FROM obd_dtcs WHERE codes IS NOT NULL "
            "AND service IN ('03', '07') ORDER BY ts, service"):
        now_set = _codes(codes)
        before = prev.get(service)
        if service == "03":
            for c in sorted(now_set - (before or set())):
                out.append({"kind": "dtc_set", "ts": ts, "code": c,
                            "title": f"Trouble code {c} stored"})
            if before is not None:
                for c in sorted(before - now_set):
                    out.append({"kind": "dtc_cleared", "ts": ts, "code": c,
                                "title": f"Trouble code {c} no longer stored"})
        else:
            stored = prev.get("03", set())
            for c in sorted(now_set - (before or set()) - stored):
                out.append({"kind": "dtc_pending", "ts": ts, "code": c,
                            "title": f"Trouble code {c} pending"})
        prev[service] = now_set
    return out


def mil_events(conn: sqlite3.Connection) -> list[dict]:
    """The engine warning light turning on, and off again (OBD mil_on or the
    import's engine_light, as cerberus.findings.engine_light reads it)."""
    out, on = [], False
    for ts, v in sorted(conn.execute(
            "SELECT ts, value FROM obd_samples WHERE name = 'mil_on' AND value IS NOT NULL "
            "UNION ALL SELECT ts, value FROM vehicle_samples "
            "WHERE name = 'engine_light' AND value IS NOT NULL").fetchall()):
        lit = v >= 1
        if lit and not on:
            out.append({"kind": "mil_on", "ts": ts, "title": "Engine warning light on"})
        elif on and not lit:
            out.append({"kind": "mil_off", "ts": ts, "title": "Engine warning light off"})
        on = lit
    return out


def reset_events(conn: sqlite3.Connection) -> list[dict]:
    """The van's service countdown jumping back up: a service reset it."""
    out, last = [], None
    for ts, v in conn.execute(
            "SELECT ts, value FROM vehicle_samples WHERE name = 'service_due_mi' "
            "AND value IS NOT NULL ORDER BY ts"):
        if last is not None and v - last > RESET_JUMP_MI:
            out.append({"kind": "service_reset", "ts": ts, "next_mi": round(v),
                        "title": "Service indicator reset",
                        "detail": f"Next service in {v:,.0f} mi (was {last:,.0f})."})
        last = v
    return out


def _day_ts(day: str, tz: tzinfo | None) -> int:
    """Noon on that day in the van's zone, so a service item sorts among the
    same day's events without pretending to know the hour."""
    return int(datetime.combine(date.fromisoformat(day), dtime(12), tzinfo=tz).timestamp())


def view(conn: sqlite3.Connection, entries: list[dict], tz: tzinfo | None = None) -> dict:
    """The timeline, newest first: the owner's items and the chassis events."""
    items = []
    for e in entries:
        items.append({**e, "kind": "service", "ts": _day_ts(e["date"], tz),
                      "title": CATEGORIES[e["category"]]})
    for ev in dtc_events(conn) + mil_events(conn) + reset_events(conn):
        ev["odometer_mi"] = odometer_at(conn, ev["ts"])
        items.append(ev)
    items.sort(key=lambda i: (i["ts"], i["kind"] == "service"), reverse=True)
    return {"items": items, "categories": CATEGORIES}
