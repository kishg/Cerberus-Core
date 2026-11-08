"""A permanent, numbered record of findings, and how each one ended.

Findings are worked out afresh from the data every time (cerberus.findings),
so on their own they have no identity and drop out of sight two weeks after
they clear. This log gives each occurrence a number that never changes and is
never reused (#14), keeps it after the data has moved on, and pairs it with
the owner's resolution note: what caused it and what fixed it. Those pairs
are what Co-Pilot learns from (its case library), so an occurrence the data
has cleared waits in "add what fixed it" until the owner writes one; nothing
closes it silently. A resolution note can also come first ("fix noted"),
with the data confirming it later.

The same finding coming back is a new occurrence with a new number, linked to
the last one, unless it returns within REOPEN_GRACE_S of clearing: then it
is the same occurrence flickering at its threshold.

The collector writes the log (the UI never writes the database). Notes stay
in the owner's notes file: a note carries the number it was written against;
older notes, from before numbers, are matched by finding and time.
"""

from __future__ import annotations

import sqlite3

from . import findings as findings_mod

REOPEN_GRACE_S = 86400
_RANK = {findings_mod.INFO: 0, findings_mod.ATTENTION: 1, findings_mod.URGENT: 2}

OPEN, FIX_NOTED, NEEDS_NOTE, CLOSED = "open", "fix_noted", "needs_note", "closed"
_COLS = ("num", "key", "title", "severity", "opened_ts", "cleared_ts", "last_seen",
         "explain", "prev_num")


def _rows(conn: sqlite3.Connection, where: str = "", args: tuple = ()) -> list[dict]:
    return [dict(zip(_COLS, r)) for r in conn.execute(
        f"SELECT {', '.join(_COLS)} FROM finding_log {where} ORDER BY num", args)]


def sync(conn: sqlite3.Connection, found: list, now: int) -> list[tuple[str, int, str]]:
    """Bring the log up to date with ``found`` (findings.derive's output).
    Returns what changed as (opened|reopened|cleared, num, key)."""
    changes = []
    open_rows = {r["key"]: r for r in _rows(conn, "WHERE cleared_ts IS NULL")}
    by_key = {f.key: f for f in found}
    for f in found:
        if f.status == findings_mod.OPEN:
            row = open_rows.get(f.key)
            if row is None:
                last = conn.execute(
                    "SELECT num, cleared_ts FROM finding_log WHERE key = ? "
                    "ORDER BY num DESC LIMIT 1", (f.key,)).fetchone()
                if last and last[1] is not None and now - last[1] <= REOPEN_GRACE_S:
                    conn.execute("UPDATE finding_log SET cleared_ts = NULL WHERE num = ?",
                                 (last[0],))
                    row = _rows(conn, "WHERE num = ?", (last[0],))[0]
                    changes.append(("reopened", row["num"], f.key))
                else:
                    opened = f.first_seen or now
                    if last and last[1] is not None:
                        opened = max(opened, last[1])
                    cur = conn.execute(
                        "INSERT INTO finding_log (key, title, severity, opened_ts, last_seen, "
                        "explain, prev_num) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (f.key, f.title, f.severity, opened, f.last_seen or now, f.explain,
                         last[0] if last else None))
                    changes.append(("opened", cur.lastrowid, f.key))
                    continue
            sev = max(row["severity"], f.severity, key=lambda s: _RANK.get(s, 0))
            conn.execute(
                "UPDATE finding_log SET title = ?, severity = ?, last_seen = ?, explain = ? "
                "WHERE num = ?",
                (f.title, sev, max(row["last_seen"] or 0, f.last_seen or now), f.explain,
                 row["num"]))
        elif not conn.execute("SELECT 1 FROM finding_log WHERE key = ?", (f.key,)).fetchone():
            # Already resolved when first seen here (the log's first run, or
            # one that opened and cleared between passes): still a case.
            cur = conn.execute(
                "INSERT INTO finding_log (key, title, severity, opened_ts, cleared_ts, "
                "last_seen, explain) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f.key, f.title, f.severity, f.first_seen or f.last_seen or now,
                 f.last_seen or now, f.last_seen or now, f.explain))
            changes.append(("cleared", cur.lastrowid, f.key))
    for key, row in open_rows.items():
        f = by_key.get(key)
        if f is None or f.status != findings_mod.OPEN:
            conn.execute("UPDATE finding_log SET cleared_ts = ? WHERE num = ?", (now, row["num"]))
            changes.append(("cleared", row["num"], key))
    return changes


def note_num(note: dict, rows: list[dict]) -> int | None:
    """The occurrence a note belongs to: the number it was written against,
    else the occurrence of its finding open at the time, else the latest one
    before it, else the first."""
    if note.get("num") is not None:
        return note["num"]
    mine = [r for r in rows if r["key"] == note["key"]]
    if not mine:
        return None
    for r in reversed(mine):
        if r["opened_ts"] <= note["ts"] and (r["cleared_ts"] is None
                                            or note["ts"] <= r["cleared_ts"] + REOPEN_GRACE_S):
            return r["num"]
    before = [r for r in mine if r["opened_ts"] <= note["ts"]]
    return (before[-1] if before else mine[0])["num"]


def state_of(row: dict, resolution: dict | None) -> str:
    if row["cleared_ts"] is None:
        return FIX_NOTED if resolution else OPEN
    return CLOSED if resolution else NEEDS_NOTE


def history(conn: sqlite3.Connection, notes: list[dict] | None = None) -> list[dict]:
    """Every occurrence, newest first, with its notes, its resolution (the
    latest resolution note) and its state."""
    try:
        rows = _rows(conn)
    except sqlite3.OperationalError:             # a database from before the log
        return []
    by_num: dict[int, list[dict]] = {}
    for n in notes or []:
        num = note_num(n, rows)
        if num is not None:
            by_num.setdefault(num, []).append(n)
    out = []
    for r in reversed(rows):
        mine = by_num.get(r["num"], [])
        res = [n for n in mine if n.get("kind") == "resolution"]
        resolution = res[-1] if res else None
        out.append({**r, "notes": mine, "resolution": resolution,
                    "state": state_of(r, resolution)})
    return out
