"""Status table: bank/pack/cell levels with 24 h change, cabin and outside
temperature/humidity range, solar peak and yield — last 24 h vs the 24 h before.

    python tools/status_report.py [--db soak/data/cerberus.db] [--config config.yaml]

Prints GitHub markdown, then the owner's recent notes on findings
(cerberus.notes): context the data can't show, to read before diagnosing. Temperatures: ambient in F, equipment in C.
battery_samples.current is per pack; the bank is the sum of packs.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime

from cerberus import notes

DAY = 86400
NOTES_DAYS = 14


def _fmt(v, nd=1, unit=""):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def _delta(a, b, nd=1, unit=""):
    if a is None or b is None:
        return "—"
    d = a - b
    return f"{d:+.{nd}f}{unit}"


def _at(db, ts, pack_id):
    """Pack (soc, voltage) at the sample nearest before ts (within 10 min)."""
    return db.execute(
        "SELECT soc, voltage FROM battery_samples WHERE pack_id=? AND ts<=? AND ts>?"
        " ORDER BY ts DESC LIMIT 1", (pack_id, ts, ts - 600)).fetchone() or (None, None)


def _net_ah(db, t0, t1):
    """Bank net Ah over [t0, t1): sum of per-pack current integrated, gaps > 5 min skipped."""
    total = 0.0
    for (pid,) in db.execute("SELECT DISTINCT pack_id FROM battery_samples"
                             " WHERE pack_id > 0 AND ts>=? AND ts<?", (t0, t1)):
        rows = db.execute("SELECT ts, current FROM battery_samples WHERE pack_id=?"
                          " AND ts>=? AND ts<? ORDER BY ts", (pid, t0, t1)).fetchall()
        for (ta, ia), (tb, _) in zip(rows, rows[1:]):
            if ia is not None and tb - ta <= 300:
                total += ia * (tb - ta) / 3600
    return total


def _range(db, sql, t0, t1):
    return db.execute(sql, (t0, t1)).fetchone()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="soak/data/cerberus.db")
    ap.add_argument("--config", default="config.yaml",
                    help="the owner's notes are read from beside this file")
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    now = int(time.time())
    a, b, c = now - 2 * DAY, now - DAY, now
    rows: list[tuple[str, str, str, str]] = []

    packs = [p for (p,) in db.execute(
        "SELECT DISTINCT pack_id FROM battery_samples WHERE pack_id>0 AND ts>? ORDER BY 1", (b,))]
    cur = {p: _at(db, now, p) for p in packs}
    ago = {p: _at(db, b, p) for p in packs}

    def bank(d, i):
        vals = [d[p][i] for p in packs if d[p][i] is not None]
        if not vals:
            return None
        return sum(vals) / len(vals)

    rows.append(("**Bank SoC**", _fmt(bank(cur, 0), 1, "%"), _fmt(bank(ago, 0), 1, "%"),
                 _delta(bank(cur, 0), bank(ago, 0), 1, "%")))
    rows.append(("Bank voltage", _fmt(bank(cur, 1), 2, " V"), _fmt(bank(ago, 1), 2, " V"),
                 _delta(bank(cur, 1), bank(ago, 1), 2, " V")))
    ah_now, ah_prev = _net_ah(db, b, c), _net_ah(db, a, b)
    rows.append(("Bank net Ah (24 h)", _fmt(ah_now, 0, " Ah"), _fmt(ah_prev, 0, " Ah"),
                 _delta(ah_now, ah_prev, 0, " Ah")))
    for p in packs:
        rows.append((f"Pack {p} SoC / V",
                     f"{_fmt(cur[p][0], 1, '%')} / {_fmt(cur[p][1], 2, ' V')}",
                     f"{_fmt(ago[p][0], 1, '%')} / {_fmt(ago[p][1], 2, ' V')}",
                     _delta(cur[p][0], ago[p][0], 1, "%")))

    # Cells come from the BLE sidecar, which only reaches the packs in the van.
    last = db.execute("SELECT MAX(ts) FROM cell_samples").fetchone()[0]
    if last is not None and now - last <= DAY:
        for p, lo, hi in db.execute(
                "SELECT pack_id, MIN(voltage_mv), MAX(voltage_mv) FROM cell_samples"
                " WHERE ts=(SELECT MAX(ts) FROM cell_samples c2 WHERE c2.pack_id=cell_samples.pack_id)"
                " GROUP BY pack_id ORDER BY pack_id"):
            rows.append((f"Pack {p} cells min–max", f"{lo}–{hi} mV (Δ{hi - lo})", "", ""))
    else:
        when = datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "never"
        rows.append(("Cells", f"no BLE data (last {when})", "", ""))

    cab = ("SELECT MIN(air_temp_f), MAX(air_temp_f), MIN(humidity_pct), MAX(humidity_pct)"
           " FROM cabin_samples WHERE ts>=? AND ts<?")
    wx = ("SELECT MIN(air_temp_f), MAX(air_temp_f), MIN(humidity_pct), MAX(humidity_pct)"
          " FROM weather_samples WHERE ts>=? AND ts<?")
    for label, sql in (("Cabin", cab), ("Outside", wx)):
        n, p = _range(db, sql, b, c), _range(db, sql, a, b)
        rows.append((f"{label} temp low / high",
                     f"{_fmt(n[0], 0, '°F')} / {_fmt(n[1], 0, '°F')}",
                     f"{_fmt(p[0], 0, '°F')} / {_fmt(p[1], 0, '°F')}",
                     _delta(n[1], p[1], 0, "°F") + " (high)"))
        rows.append((f"{label} humidity low / high",
                     f"{_fmt(n[2], 0, '%')} / {_fmt(n[3], 0, '%')}",
                     f"{_fmt(p[2], 0, '%')} / {_fmt(p[3], 0, '%')}", ""))

    sol = "SELECT MAX(pv_power) FROM solar_samples WHERE ts>=? AND ts<?"
    pn, pp = _range(db, sol, b, c)[0], _range(db, sol, a, b)[0]
    rows.append(("Solar max", _fmt(pn, 0, " W"), _fmt(pp, 0, " W"), _delta(pn, pp, 0, " W")))
    # Daily yield: last value of yield_today per calendar day (resets at midnight).
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    ys = dict(db.execute(
        "SELECT date(ts,'unixepoch','localtime') d, MAX(yield_today_kwh) FROM solar_samples"
        " WHERE ts>=? GROUP BY d", (now - 3 * DAY,)).fetchall())
    yd = sorted(ys)
    yn = ys.get(today)
    yp = ys.get(yd[-2]) if len(yd) >= 2 and yd[-1] == today else None
    rows.append(("Solar yield (today vs yesterday)", _fmt(yn and yn * 1000, 0, " Wh"),
                 _fmt(yp and yp * 1000, 0, " Wh"),
                 _delta(yn and yn * 1000, yp and yp * 1000, 0, " Wh")))

    print(f"As of {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')} "
          "(last 24 h vs previous 24 h)\n")
    print("| Metric | Now / last 24 h | Previous 24 h | Change |")
    print("|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")

    recent = [n for n in notes.read(args.config) if n["ts"] >= now - NOTES_DAYS * DAY]
    if recent:
        print(f"\nOwner notes (last {NOTES_DAYS} days):\n")
        for n in recent:
            when = datetime.fromtimestamp(n["ts"]).strftime("%m-%d %H:%M")
            tag = " (marked known)" if n["known"] else ""
            print(f"- {when} on *{n['title'] or n['key']}*{tag}: {n['text']}")


if __name__ == "__main__":
    main()
