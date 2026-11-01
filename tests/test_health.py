"""Step 5.3: data health — source freshness, coverage, gaps and their causes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cerberus import health
from cerberus.db import Db

NY = ZoneInfo("America/New_York")
NOW = int(datetime(2026, 10, 31, 15, 0, tzinfo=NY).timestamp())
H = 3600


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "h.db"))


def _battery(db, a, b, step=30, source="mqtt"):
    for t in range(a, b, step):
        db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current, source) "
                        "VALUES (?, 1, 90, 13.3, 0, ?)", (t, source))


def _weather(db, a, b):
    for t in range(a, b, 900):
        db.conn.execute("INSERT INTO weather_samples (ts, air_temp_f) VALUES (?, 50)", (t,))


def _uptime(db, a, b):
    for t in range(a, b, 300):
        db.mark_uptime(t)


def _gap(db, cause_setup=None):
    """Data, a 2-hour hole from NOW-4h to NOW-2h, then data again."""
    _battery(db, NOW - 6 * H, NOW - 4 * H)
    if cause_setup:
        cause_setup(NOW - 4 * H, NOW - 2 * H)
    _battery(db, NOW - 2 * H, NOW)
    [g] = health.gaps(db.conn, NOW)
    assert (g["start"], g["end"]) == (NOW - 4 * H - 30, NOW - 2 * H)
    return g


# --- causes ----------------------------------------------------------------------

def test_cerbo_unreachable_when_cerberus_kept_fetching_weather(db):
    db.conn.execute("INSERT INTO cerbo_wifi_samples (ts, signal_strength) VALUES (?, 31)", (NOW - 5 * H,))
    g = _gap(db, lambda a, b: _weather(db, a, b))
    assert g["cause"] == "cerbo" and "WiFi signal was 31%" in g["explain"]


def test_van_network_down_when_running_but_offline(db):
    _uptime(db, NOW - 7 * H, NOW)                  # heartbeat throughout, no weather
    assert _gap(db)["cause"] == "network"


def test_restart_after_being_off(db):
    _uptime(db, NOW - 7 * H, NOW - 4 * H)
    _uptime(db, NOW - 2 * H, NOW)
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, 'collector_started', 'info', '{}')",
                    (NOW - 2 * H - 20,))
    assert _gap(db)["cause"] == "restart"


def test_asleep_when_nothing_written_and_no_restart(db):
    _uptime(db, NOW - 7 * H, NOW - 4 * H)
    _uptime(db, NOW - 2 * H, NOW)
    assert _gap(db)["cause"] == "asleep"


def test_before_uptime_records_it_says_it_cannot_tell(db):
    g = _gap(db)
    assert g["cause"] == "stopped" and "can't tell these apart" in g["explain"]


def _cells(db, a, b, step=30):
    for t in range(a, b, step):
        db.conn.execute("INSERT INTO cell_samples (ts, pack_id, cell_idx, voltage_mv) "
                        "VALUES (?, 1, 1, 3300)", (t,))


def test_bluetooth_rows_mean_the_computer_was_awake_so_the_network_was_down(db):
    """10-22 on this van: inverter off at 23:00 took the Starlink router with it,
    while the BLE sidecar kept writing cells. Before uptime rows existed."""
    g = _gap(db, lambda a, b: _cells(db, a, b))
    assert g["cause"] == "network" and "Bluetooth readings kept arriving" in g["explain"]
    assert "stopped collector looks the same" in g["explain"]


def test_awake_but_no_heartbeat_means_the_collector_was_not_running(db):
    _uptime(db, NOW - 7 * H, NOW - 4 * H)
    _uptime(db, NOW - 2 * H, NOW)
    assert _gap(db, lambda a, b: _cells(db, a, b))["cause"] == "stopped"


def test_a_few_bluetooth_rows_are_not_enough(db):
    _cells(db, NOW - 4 * H, NOW - 4 * H + 1800)    # 30 min of a 2-hour gap
    assert _gap(db)["cause"] == "stopped"


def test_partly_running(db):
    _weather(db, NOW - 4 * H, NOW - 3 * H)         # online for the first half only
    assert _gap(db)["cause"] == "partial"


# --- what counts --------------------------------------------------------------------

def test_vrm_backfill_does_not_fill_a_gap(db):
    _battery(db, NOW - 4 * H, NOW - 2 * H, step=900, source="vrm")
    assert _gap(db)["cause"] == "stopped"


def test_ongoing_gap_has_no_end(db):
    _battery(db, NOW - 3 * H, NOW - H)
    [g] = health.gaps(db.conn, NOW)
    assert g["end"] is None and g["duration_s"] == H + 30


def test_short_holes_are_not_gaps(db):
    _battery(db, NOW - 2 * H, NOW - H)
    _battery(db, NOW - H + 300, NOW)
    assert health.gaps(db.conn, NOW) == []


def test_a_night_of_dropouts_is_one_episode(db):
    """10-27 on this van: nine Cerbo dropouts between 01:06 and 06:24."""
    t = NOW - 10 * H
    for _ in range(4):
        _battery(db, t, t + 1200)
        _weather(db, t + 1200, t + 2400)
        t += 2400
    _battery(db, t, NOW)
    v = health.health_view(db.conn, NOW, NY)
    [e] = v["episodes"]
    assert (e["cause"], e["count"]) == ("cerbo", 4) and v["missing_s"] == pytest.approx(4 * 1230)


# --- sources and coverage -------------------------------------------------------------

def test_sources_fresh_stale_and_out_of_range(db):
    _battery(db, NOW - 60, NOW)
    db.conn.execute("INSERT INTO weather_samples (ts, air_temp_f) VALUES (?, 50)", (NOW - 3 * H,))
    db.conn.execute("INSERT INTO pack_ble_samples (ts, pack_id) VALUES (?, 1)", (NOW - 2 * 86400,))
    s = {r["key"]: r["status"] for r in health.sources(db.conn, NOW)}
    assert s == {"battery": "fresh", "weather": "stale", "packs_ble": "away"}   # never-seen hidden


def test_coverage_is_per_van_local_day(db):
    midnight = int(datetime(2026, 10, 31, 0, 0, tzinfo=NY).timestamp())
    _battery(db, midnight, midnight + 6 * H)            # 6 of the first 15 h today
    cov = dict(health.coverage(db.conn, NOW, NY, days=2))
    assert cov == {"2026-10-30": 0.0, "2026-10-31": 40.0}


def test_database_without_uptime_table_still_works(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE battery_samples (ts INT, pack_id INT, source TEXT DEFAULT 'mqtt');"
                    "CREATE TABLE weather_samples (ts INT);"
                    "CREATE TABLE events (ts INT, type TEXT, severity TEXT, detail TEXT);"
                    "CREATE TABLE cabin_samples (ts INT, source TEXT);"
                    "CREATE TABLE cerbo_wifi_samples (ts INT, signal_strength INT);")
    for t in list(range(NOW - 6 * H, NOW - 4 * H, 30)) + list(range(NOW - 2 * H, NOW, 30)):
        c.execute("INSERT INTO battery_samples (ts, pack_id) VALUES (?, 1)", (t,))
    v = health.health_view(c, NOW, NY)
    assert v["episodes"][0]["cause"] == "stopped"
