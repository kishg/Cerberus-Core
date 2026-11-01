"""Daily solar charge-path check (cerberus.solar_path).

Synthetic days shaped like the real ones: a morning ramp in bulk, the MPPT
reaching its 14.4 V absorption setpoint at its own terminals while the packs
sit near 13.35 V, then a float. Path resistance R puts the MPPT terminals at
V_pack + I*R.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

from cerberus import solar_path as sp
from cerberus.db import Db
from cerberus.events import Severity

DAY = "2026-10-05"
# This rig: three packs per whole-bank sample.
RIG = sp.Settings(pack_count=3)


def _db() -> Db:
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def _ts(hhmm: str, day: str = DAY) -> int:
    return int(datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp())


def _sample(db, ts, amps, state, r_ohm, v_pack=13.35, soc=90.0, alt=0.0,
            log_dc=True):
    for pack in (1, 2, 3):
        db.conn.execute(
            "INSERT INTO battery_samples (ts, pack_id, soc, voltage, current) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, pack, soc, v_pack, (amps + alt) / 3),
        )
    db.insert_solar_sample(
        ts=ts, pv_power=amps * 14, pv_voltage=18.0, charge_current=amps,
        yield_today_kwh=0.5, state=state, error_code=0,
        dc_voltage=(v_pack + amps * r_ohm) if log_dc else None,
    )


def _day(db, r_ohm=0.055, float_soc=86.0, day=DAY, log_dc=True, alt=0.0):
    """Ramp 4 -> 20 A in bulk from 08:00, absorption when the MPPT terminals
    reach 14.4 V, float two hours later. A path too low to ever reach 14.4 V
    stays in bulk until 15:00, then floats."""
    t = _ts("08:00", day)
    stop = _ts("15:00", day)
    amps = 4.0
    state = 3
    while t < stop:
        if state == 3 and 13.35 + amps * r_ohm >= sp.ABSORPTION_V:
            state = 4
            absorb_t = t
        if state == 4 and t >= absorb_t + 7200:
            break
        _sample(db, t, amps, state, r_ohm, soc=80.0, alt=alt, log_dc=log_dc)
        t += 30
        amps = min(amps + 0.1, 20.0)
    _sample(db, t, 0.6, 5, r_ohm, v_pack=13.33, soc=float_soc, log_dc=log_dc)


def test_measured_path_resistance():
    db = _db()
    _day(db, r_ohm=0.055)
    m = sp.compute_day(db, DAY, RIG)
    assert abs(m["path_mohm"] - 55) < 1
    assert abs(m["path_mohm_slope"] - 55) < 1


def test_slope_removes_a_calibration_offset():
    """A fixed 50 mV offset between MPPT and BMS biases the ratio, not the slope."""
    db = _db()
    _day(db, r_ohm=0.055)
    db.conn.execute("UPDATE solar_samples SET dc_voltage = dc_voltage + 0.05")
    m = sp.compute_day(db, DAY, RIG)
    assert m["path_mohm"] > 56.5                 # +50 mV / ~20 A = +2.5
    assert abs(m["path_mohm_slope"] - 55) < 1


def test_inferred_resistance_at_absorption_needs_no_dc_voltage():
    """History before 2026-10-03 has no dc_voltage; the absorption moment
    still gives the path."""
    db = _db()
    _day(db, r_ohm=0.080, log_dc=False)
    m = sp.compute_day(db, DAY, RIG)
    assert "path_mohm" not in m
    assert abs(m["path_mohm_at_absorption"] - 80) < 3


def test_alternator_charging_is_excluded():
    db = _db()
    _day(db, r_ohm=0.055, alt=150.0)
    m = sp.compute_day(db, DAY, RIG)
    assert m["path_samples"] == 0
    assert "path_mohm_at_absorption" not in m


def test_early_float_flags():
    db = _db()
    _day(db, r_ohm=0.055, float_soc=86.0)
    m = sp.compute_day(db, DAY, RIG)
    flags = sp.evaluate(m, [])
    assert [f["metric"] for f in flags] == ["soc_at_float"]


def test_full_float_with_low_resistance_is_normal():
    db = _db()
    _day(db, r_ohm=0.040, float_soc=99.5)
    m = sp.compute_day(db, DAY, RIG)
    assert sp.evaluate(m, []) == []


def test_high_resistance_flags_on_both_estimates():
    db = _db()
    _day(db, r_ohm=0.085, float_soc=99.5)
    flags = sp.evaluate(sp.compute_day(db, DAY, RIG), [], RIG)
    assert {f["metric"] for f in flags} == {"path_mohm", "path_mohm_at_absorption"}


def test_drift_flags_against_baseline():
    hist = [{"path_mohm": 50.0 + (k % 3)} for k in range(10)]
    m = {"path_mohm": 70.0}
    flags = sp.evaluate(m, hist)
    assert [f["kind"] for f in flags] == ["anomaly"]


def test_day_is_processed_after_dark_and_only_once():
    db = _db()
    _day(db, day="2026-10-04")
    sp.process(db, _ts("22:00", "2026-10-04"), settings=RIG)          # first run: silent
    _day(db, r_ohm=0.085, day=DAY)
    assert sp.process(db, _ts("15:00"), settings=RIG) == []             # sun still up
    events = sp.process(db, _ts("21:30"), settings=RIG)
    assert len(events) == 1
    assert events[0].type == "solar_path_report"
    assert events[0].severity == Severity.WARN
    assert sp.process(db, _ts("23:00"), settings=RIG) == []
    row = db.conn.execute("SELECT metrics FROM solar_days WHERE day = ?", (DAY,)).fetchone()
    assert json.loads(row[0])["path_mohm"] > 80


def test_no_sun_no_row():
    db = _db()
    _sample(db, _ts("12:00"), 0.0, 0, 0.05)
    assert sp.compute_day(db, DAY, RIG) is None
