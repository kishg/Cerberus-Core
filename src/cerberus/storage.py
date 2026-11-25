"""Storage mode: the owner says the van is stored (Core #54).

Vans spend weeks or months stored: parked, often offline, sometimes with the
batteries switched off for winter. Without being told, Cerberus can't tell that
from a fault. So the owner says so, and Cerberus adjusts:

- **Gaps and blackouts** while stored are explained as "in storage", not as a
  fault (cerberus.health).
- **Findings a stored van trips by design** (``QUIET_IN_STORAGE``: no full
  charge, early float, solar-path resistance, SoC reading high) can't open a
  new occurrence. One already open stays open; storage fixes nothing.
  Everything that protects a stored van stays on: battery alarms, the
  chassis battery, cell imbalance, temperatures.
- **Phone fixes** don't move the van while it's stored, unless it's moving.
- **A drive ends storage** (engine charging or running), and says so.

The state lives in a small file beside config.yaml (``STORAGE_FILE``), like
notes: the UI can write files but not the database. The collector turns each
change into a dated event (``storage_started`` / ``storage_ended``), which
health and sync use. It is suggested, never switched on silently.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

STORAGE_FILE = ".cerberus-storage.json"
QUIET_IN_STORAGE = frozenset({"no_full_charge", "early_float", "solar_path_resistance", "soc_reads_high"})
SUGGEST_AFTER_DAYS = 7          # parked this long with no drive: ask
DAY = 86400


class StorageError(ValueError):
    pass


@dataclass(frozen=True)
class State:
    on: bool = False
    since: int | None = None        # when it was switched on
    until: str | None = None        # expected end, YYYY-MM-DD (optional)
    online: bool = False            # stays online in storage (shore power and internet)
    by: str = ""                    # who changed it last: owner, drive, cloud, ha
    changed_at: int | None = None
    ended_reason: str | None = None

    def due_passed(self, today: date) -> bool:
        return self.on and self.until is not None and date.fromisoformat(self.until) < today


def path_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(STORAGE_FILE)


def read(config_path: str | Path) -> State:
    try:
        raw = json.loads(path_for(config_path).read_text())
    except (FileNotFoundError, ValueError):
        return State()
    if not isinstance(raw, dict):
        return State()
    fields = {k: raw.get(k) for k in State.__dataclass_fields__ if k in raw}
    try:
        return State(**fields)
    except TypeError:
        return State()


def _write(config_path: str | Path, state: State) -> State:
    p = path_for(config_path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".storage-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=1)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return state


def start(config_path: str | Path, now: int, *, until: str | None = None, online: bool = False,
          by: str = "owner") -> State:
    if until is not None and until != "":
        try:
            until = date.fromisoformat(str(until)).isoformat()
        except ValueError:
            raise StorageError("until must be a date like 2027-03-01") from None
    else:
        until = None
    current = read(config_path)
    since = current.since if current.on and current.since else now
    return _write(config_path, State(on=True, since=since, until=until, online=bool(online), by=by,
                                     changed_at=now))


def end(config_path: str | Path, now: int, *, reason: str = "owner", by: str = "owner") -> State:
    current = read(config_path)
    if not current.on:
        return current
    return _write(config_path, State(on=False, since=current.since, by=by, changed_at=now,
                                     ended_reason=reason))


# -- what the collector and the UI need ------------------------------------------

def spans(conn: sqlite3.Connection, now: int) -> list[tuple[int, int]]:
    """Stored periods from the storage events, the last one up to now if still on."""
    try:
        rows = conn.execute("SELECT ts, type FROM events WHERE type IN ('storage_started', 'storage_ended') "
                            "ORDER BY ts, rowid").fetchall()
    except sqlite3.Error:
        return []
    out, start_ts = [], None
    for ts, kind in rows:
        if kind == "storage_started" and start_ts is None:
            start_ts = ts
        elif kind == "storage_ended" and start_ts is not None:
            out.append((start_ts, ts))
            start_ts = None
    if start_ts is not None:
        out.append((start_ts, now))
    return out


def stored_during(conn: sqlite3.Connection, a: int, b: int, now: int) -> float:
    """The fraction of a..b inside a stored period."""
    if b <= a:
        return 0.0
    covered = sum(max(0, min(b, e) - max(a, s)) for s, e in spans(conn, now))
    return covered / (b - a)


def suggestion(conn: sqlite3.Connection, now: int, state: State) -> str | None:
    """Why the van looks stored, when storage mode is off; else None. Never acted on silently."""
    if state.on:
        return None
    def one(sql, args=()):
        try:
            row = conn.execute(sql, args).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None
    last_drive = one("SELECT MAX(end_ts) FROM drives")
    last_engine = one("SELECT MAX(ts) FROM engine_samples WHERE rpm > 0")
    last_moved = max([t for t in (last_drive, last_engine) if t] or [0])
    first_data = one("SELECT MIN(ts) FROM battery_samples")
    if not first_data or now - first_data < SUGGEST_AFTER_DAYS * DAY:
        return None                 # too new a box to judge
    if last_moved and now - last_moved >= SUGGEST_AFTER_DAYS * DAY:
        return f"No drive for {(now - last_moved) // DAY} days."
    if not last_moved and now - first_data >= SUGGEST_AFTER_DAYS * DAY:
        return f"No drive recorded in {(now - first_data) // DAY} days."
    return None


def view(conn: sqlite3.Connection | None, config_path: str | Path, now: int, today: date) -> dict:
    state = read(config_path)
    return {**asdict(state), "due_passed": state.due_passed(today),
            "suggest": suggestion(conn, now, state) if conn is not None else None}


def quiet(found: list, open_keys: set[str], state: State) -> list:
    """Findings as the log should see them while stored: a quiet one can't open a new occurrence,
    but one already open passes through unchanged (so it isn't marked as cleared)."""
    if not state.on:
        return found
    return [f for f in found if f.key not in QUIET_IN_STORAGE or f.key in open_keys]
