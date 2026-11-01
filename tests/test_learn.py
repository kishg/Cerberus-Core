"""Step 5.5: settings learned from the van's own history (cerberus.learn)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cerberus import learn, settings
from cerberus.db import Db

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DAY = 86400
NOW = 1_793_480_000


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "l.db"))


def _solar_day(db, day, idle_v, harvest_v=17.5):
    """A day: harvesting at Vmp, then idle at open circuit for a while."""
    t0 = NOW - day * DAY + 15 * 3600
    for k in range(20):
        db.conn.execute("INSERT INTO solar_samples (ts, pv_power, pv_voltage, charge_current) "
                        "VALUES (?, 150, ?, 10)", (t0 + 30 * k, harvest_v))
    for k in range(20):
        db.conn.execute("INSERT INTO solar_samples (ts, pv_power, pv_voltage, charge_current) "
                        "VALUES (?, 0, ?, 0)", (t0 + 900 + 30 * k, idle_v))


def _session(db, start, amps, minutes=20, soc=60.0, temps=None):
    """One pack (pack_count 1), charging at `amps` from something that isn't
    solar; `temps` = {instance: (start_c, end_c)} rising linearly."""
    n = minutes * 2
    for k in range(n):
        ts = start + 30 * k
        db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current, power, "
                        "temperature, source) VALUES (?, 1, ?, 13.6, ?, 0, 25, 'mqtt')",
                        (ts, soc, amps))
        for inst, (a, b) in (temps or {}).items():
            db.conn.execute("INSERT INTO temperature_samples (ts, instance, temperature_c) "
                            "VALUES (?, ?, ?)", (ts, inst, a + (b - a) * k / (n - 1)))


def _drives(db, count, amps=180.0, rise=20.0):
    for d in range(count):
        _session(db, NOW - (d + 1) * DAY, amps, temps={24: (30, 30 + rise), 7: (21, 22)})


# --- array Voc ----------------------------------------------------------------------

def test_voc_learned_from_idle_pv(db):
    for d, v in enumerate([20.1, 20.4, 20.6, 21.9, 20.5, 20.3, 19.8, 20.7, 20.9], start=1):
        _solar_day(db, d, v)
    _solar_day(db, 10, 3.0)                             # a dark day doesn't count
    p = learn.array_voc(db.conn, NOW)
    assert p["value"] == 20.5 and "9 sunny days" in p["reason"]


def test_voc_learning_until_a_week_of_sun(db):
    for d in range(1, 4):
        _solar_day(db, d, 20.0)
    p = learn.array_voc(db.conn, NOW)
    assert p["learning"] and "3 of 7" in p["reason"]


def test_no_charge_controller_no_voc(db):
    assert learn.array_voc(db.conn, NOW) is None


# --- engine charging current -------------------------------------------------------

def test_engine_current_from_the_upper_cluster(db):
    _drives(db, 4, amps=180.0)
    for d in range(1, 8):                               # morning solar the MPPT under-reports
        _session(db, NOW - d * DAY + 6 * 3600, 11.0, minutes=60)
    _session(db, NOW - 10 * DAY, 190.0, soc=95.0)       # a full bank: not informative
    p = learn.engine_charge(learn.engine_sessions(db.conn, NOW, 1))
    assert p["value"] == 180 and "4 drives" in p["reason"]


def test_engine_current_learning(db):
    _drives(db, 2)
    p = learn.engine_charge(learn.engine_sessions(db.conn, NOW, 1))
    assert p["learning"] and "2 of 3" in p["reason"]


def test_a_dc_dc_charger_is_learned_too(db):
    """A 30 A DC-DC charger would never be a 'drive' at the 200 A default:
    learning must not depend on drive detection."""
    for d in range(1, 5):
        _session(db, NOW - d * DAY, 30.0)
    assert learn.engine_charge(learn.engine_sessions(db.conn, NOW, 1))["value"] == 30


# --- joint probe --------------------------------------------------------------------

def test_the_probe_that_heats_is_the_joint(db):
    _drives(db, 4, rise=20.0)
    p = learn.joint_probe(db.conn, learn.engine_sessions(db.conn, NOW, 1))
    assert p["value"] == 24 and "rises 20 °C" in p["reason"]


def test_no_probe_heats(db):
    _drives(db, 4, rise=1.0)
    p = learn.joint_probe(db.conn, learn.engine_sessions(db.conn, NOW, 1))
    assert p["value"] is None and p["display"] == "None" and not p.get("learning")


def test_no_probes_at_all(db):
    for d in range(1, 5):
        _session(db, NOW - d * DAY, 180.0)
    assert learn.joint_probe(db.conn, learn.engine_sessions(db.conn, NOW, 1)) is None


# --- through settings ---------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, p)
    return p


def test_learned_values_match_within_tolerance(cfg, db):
    for d, v in enumerate([20.6] * 8, start=1):
        _solar_day(db, d, v)
    _drives(db, 4, amps=196.0)
    rows = {r["key"]: r for r in settings.current(cfg, learn.proposals(db.conn, NOW, 1))}
    assert rows["solar.array_voc_v"]["detected"]["matches"] is True      # 20.6 vs 20.0: within 5%
    assert rows["drives.engine_charge_a"]["detected"]["matches"] is True # 195 vs 200
    assert rows["drives.joint_temp_instance"]["detected"]["matches"] is True


def test_learning_never_matches_or_differs(cfg, db):
    _drives(db, 1)
    rows = {r["key"]: r for r in settings.current(cfg, learn.proposals(db.conn, NOW, 1))}
    assert rows["drives.engine_charge_a"]["detected"]["matches"] is None


def test_using_a_learned_joint_of_none(cfg):
    rows = {r["key"]: r for r in settings.apply(cfg, {"drives.joint_temp_instance": None})}
    assert (rows["drives.joint_temp_instance"]["value"], rows["drives.joint_temp_instance"]["source"]) == (None, "setup")
