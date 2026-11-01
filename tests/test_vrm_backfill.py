"""VRM backfill into solar_samples / ac_samples (tools/vrm_backfill.py).

Unlike battery rows, which go to pack_id 0, solar and AC VRM rows share the
collector's ts key space, so the provenance column and the clash guard are
what keep them from passing as, or overwriting, live 30s samples.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile

from cerberus.db import Db

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "vrm_backfill.py")
T0 = 1791110700  # 2026-10-04 06:45 EDT, the first real backfilled bucket


def _db_path() -> str:
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    Db(path).close()
    return path


def _csv(text: str) -> str:
    path = os.path.join(tempfile.mkdtemp(), "w.csv")
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)


SOLAR = "# comment\npvp,pvv,sci,scv,yt\n0,16.5,0,13.28,0\n114.6,16.72,8,14.02,0.08\n"
AC = "s\n0\n0\n"


def test_migration_adds_source_to_pre_existing_tables():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE solar_samples (ts INTEGER PRIMARY KEY, pv_power REAL, "
        "pv_voltage REAL, charge_current REAL, yield_today_kwh REAL, state INTEGER, "
        "error_code INTEGER);"
        "CREATE TABLE ac_samples (ts INTEGER PRIMARY KEY, consumption_w REAL, "
        "input_w REAL, output_w REAL, shore_connected INTEGER, inverter_state INTEGER);"
        "INSERT INTO solar_samples (ts, pv_power) VALUES (1, 5.0);"
        "INSERT INTO ac_samples (ts, inverter_state) VALUES (1, 0);"
    )
    conn.commit()
    conn.close()

    db = Db(path)
    assert db.conn.execute("SELECT source FROM solar_samples").fetchone() == ("mqtt",)
    assert db.conn.execute("SELECT source FROM ac_samples").fetchone() == ("mqtt",)


def test_live_inserts_keep_mqtt_default():
    db = Db(_db_path())
    db.insert_solar_sample(ts=1, pv_power=1, pv_voltage=18, charge_current=0.1,
                           yield_today_kwh=0, state=3, error_code=0)
    db.insert_ac_sample(ts=1, consumption_w=None, input_w=None, output_w=None,
                        shore_connected=None, inverter_state=0)
    assert db.conn.execute("SELECT source FROM solar_samples").fetchone() == ("mqtt",)
    assert db.conn.execute("SELECT source FROM ac_samples").fetchone() == ("mqtt",)


def test_solar_import_maps_columns_and_leaves_state_null():
    path = _db_path()
    r = _run(_csv(SOLAR), "--db", path, "--first-ts", str(T0), "--table", "solar", "--commit")
    assert r.returncode == 0, r.stderr
    rows = sqlite3.connect(path).execute(
        "SELECT ts, pv_power, pv_voltage, charge_current, yield_today_kwh, state, "
        "error_code, dc_voltage, source FROM solar_samples ORDER BY ts"
    ).fetchall()
    assert rows == [
        (T0, 0.0, 16.5, 0.0, 0.0, None, None, 13.28, "vrm"),
        (T0 + 900, 114.6, 16.72, 8.0, 0.08, None, None, 14.02, "vrm"),
    ]


def test_ac_import_writes_only_inverter_state():
    path = _db_path()
    r = _run(_csv(AC), "--db", path, "--first-ts", str(T0), "--table", "ac", "--commit")
    assert r.returncode == 0, r.stderr
    rows = sqlite3.connect(path).execute("SELECT * FROM ac_samples ORDER BY ts").fetchall()
    assert rows == [(T0, None, None, None, None, 0, "vrm", None, None),
                    (T0 + 900, None, None, None, None, 0, "vrm", None, None)]


def test_refuses_to_overwrite_collector_rows():
    path = _db_path()
    db = Db(path)
    db.insert_solar_sample(ts=T0 + 900, pv_power=120, pv_voltage=17, charge_current=8.5,
                           yield_today_kwh=0.08, state=3, error_code=0, dc_voltage=14.0)
    db.close()

    r = _run(_csv(SOLAR), "--db", path, "--first-ts", str(T0), "--table", "solar", "--commit")
    assert r.returncode == 1
    assert "refusing" in r.stderr
    rows = sqlite3.connect(path).execute("SELECT ts, state, source FROM solar_samples").fetchall()
    assert rows == [(T0 + 900, 3, "mqtt")]


def test_rerun_over_own_rows_is_idempotent():
    path = _db_path()
    args = (_csv(SOLAR), "--db", path, "--first-ts", str(T0), "--table", "solar", "--commit")
    assert _run(*args).returncode == 0
    assert _run(*args).returncode == 0
    n = sqlite3.connect(path).execute("SELECT COUNT(*) FROM solar_samples").fetchone()[0]
    assert n == 2
