"""Status table (tools/status_report.py): last 24 h vs the 24 h before.

What to pin: the bank change is the pack mean now minus 24 h ago, net Ah sums
the per-pack currents (the bank is 3x one pack), and a missing BLE cell feed
says so instead of printing stale cells.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from cerberus.db import Db

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "status_report.py")


def _seed() -> str:
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    Db(path).close()
    import sqlite3
    db = sqlite3.connect(path)
    now = int(time.time())
    # Each pack: 90% 24 h ago, 87% now, -1 A for the last hour (30 s steps).
    for p in (1, 2, 3):
        db.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current)"
                   " VALUES (?, ?, 90.0, 13.30, 0.0)", (now - 86400 - 10, p))
        for t in range(now - 3600, now, 30):
            db.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current)"
                       " VALUES (?, ?, 87.0, 13.25, -1.0)", (t, p))
    db.execute("INSERT INTO solar_samples (ts, pv_power, yield_today_kwh) VALUES (?, 203, 0.62)",
               (now - 60,))
    db.execute("INSERT INTO weather_samples (ts, air_temp_f, humidity_pct) VALUES (?, 52, 83)",
               (now - 120,))
    db.commit()
    db.close()
    return path


def _row(out: str, label: str) -> list[str]:
    line = next(l for l in out.splitlines() if l.startswith(f"| {label}"))
    return [c.strip() for c in line.strip("|").split("|")]


def test_table_rows():
    out = subprocess.run([sys.executable, TOOL, "--db", _seed()],
                         capture_output=True, text=True, check=True).stdout
    assert _row(out, "**Bank SoC**")[1:] == ["87.0%", "90.0%", "-3.0%"]
    # 3 packs x -1 A x ~1 h
    assert _row(out, "Bank net Ah")[1] == "-3 Ah"
    assert _row(out, "Pack 2 SoC / V")[3] == "-3.0%"
    assert _row(out, "Cells")[1] == "no BLE data (last never)"
    assert _row(out, "Outside temp low / high")[1] == "52°F / 52°F"
    assert _row(out, "Solar max")[1] == "203 W"


def test_owner_notes_print_under_the_table_with_known_marked():
    from cerberus import notes
    path = _seed()
    cfg = os.path.join(os.path.dirname(path), "config.yaml")
    open(cfg, "w").close()
    now = int(time.time())
    notes.add(cfg, "inverter_low_battery", "Heater runs at night", "Inverter low battery",
              now=now - 3600, known=True)
    notes.add(cfg, "early_float", "Checked absorption", "Early float", now=now - 1800)
    notes.add(cfg, "early_float", "Too old to print", "Early float", now=now - 30 * 86400)
    out = subprocess.run([sys.executable, TOOL, "--db", path, "--config", cfg],
                         capture_output=True, text=True, check=True).stdout
    assert "Owner notes (last 14 days)" in out and "Too old" not in out
    assert "*Inverter low battery* (marked known): Heater runs at night" in out
    assert "*Early float*: Checked absorption" in out


def test_reads_without_writing():
    """--db may point at the live collector's database: read-only, and a
    wrong path fails instead of leaving an empty database behind."""
    path = _seed()
    before = os.stat(path).st_mtime_ns
    subprocess.run([sys.executable, TOOL, "--db", path], capture_output=True, check=True)
    assert os.stat(path).st_mtime_ns == before
    missing = os.path.join(os.path.dirname(path), "nope.db")
    r = subprocess.run([sys.executable, TOOL, "--db", missing], capture_output=True, text=True)
    assert r.returncode != 0 and not os.path.exists(missing)


def test_solar_days_are_the_vans_not_the_hosts():
    """The box runs in UTC; at 9 pm in New York it is already tomorrow there."""
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from cerberus import status
    ny = ZoneInfo("America/New_York")
    now = int(datetime(2026, 11, 20, 21, 0, tzinfo=ny).timestamp())
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    Db(path).close()
    db = sqlite3.connect(path)
    for ts, y in ((now - 86400, 0.40), (now - 3600, 0.38)):   # yesterday and today, NY
        db.execute("INSERT INTO solar_samples (ts, pv_power, yield_today_kwh) VALUES (?, 0, ?)",
                   (ts, y))
    view = status.status_view(db, now, ny)
    row = next(r for r in view["rows"] if r[0].startswith("Solar yield"))
    assert row[1:] == ["380 Wh", "400 Wh", "-20 Wh"]


def test_status_over_the_api():
    """GET /api/v1/status serves the table and notes; --url prints it."""
    import threading
    from cerberus import notes
    from cerberus.ui.server import App, serve
    path = _seed()
    cfg = os.path.join(os.path.dirname(path), "config.yaml")
    open(cfg, "w").close()
    notes.add(cfg, "early_float", "Checked absorption", "Early float", now=int(time.time()) - 60)
    app = App(path, 3, 310, "America/New_York", config_path=cfg)
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        out = subprocess.run([sys.executable, TOOL, "--url", url],
                             capture_output=True, text=True, check=True).stdout
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert _row(out, "**Bank SoC**")[1:] == ["87.0%", "90.0%", "-3.0%"]
    assert "*Early float*: Checked absorption" in out
