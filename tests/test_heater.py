"""Water-heater run detection (cerberus.heater).

Synthetic loads shaped like the 10-20 run: ~120 W base, a 1.44 kW step, the
inverter at V_pack - I*R with R = 4.2 mOhm, packs sampled 6 s after the AC.
"""
from __future__ import annotations

import json
import os
import tempfile

from cerberus import heater
from cerberus.db import Db
from cerberus.events import Severity, make

T0 = 1792521000
# This rig: three packs per whole-bank sample.
RIG = heater.Settings(pack_count=3)
R = 0.0042


def _db() -> Db:
    db = Db(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.set_kv(heater.KV_THROUGH, str(T0))
    return db


def _sample(db, ts, w, v_pack=13.15, soc=90.0):
    amps = (w + 30) / 12.5 * 1.15 if w > 500 else 10.0
    db.insert_ac_sample(ts=ts, consumption_w=w, input_w=0, output_w=w,
                        shore_connected=False, inverter_state=9,
                        inverter_dc_voltage=v_pack - amps * R,
                        inverter_dc_current=-amps)
    for pack in (1, 2, 3):
        db.conn.execute(
            "INSERT INTO battery_samples (ts, pack_id, soc, voltage, current) "
            "VALUES (?, ?, ?, ?, ?)", (ts + 6, pack, soc, v_pack, -amps / 3))


def _load(db, start, base_min=15, on_min=13, after_min=5, w_on=1440.0):
    t = start
    for _ in range(base_min * 2):
        _sample(db, t, 120.0); t += 30
    on_ts = t
    for _ in range(on_min * 2):
        _sample(db, t, w_on); t += 30
    off_ts = t
    for _ in range(after_min * 2):
        _sample(db, t, 120.0); t += 30
    return on_ts, off_ts, t


def _types(evs):
    return [e.type for e in evs]


def test_first_run_starts_cursor_without_replaying():
    db = Db(os.path.join(tempfile.mkdtemp(), "t.db"))
    _load(db, T0)
    assert heater.process(db, T0 + 3600, RIG) == []
    assert db.get_kv(heater.KV_THROUGH) == str(T0 + 3600)


def test_one_run_on_and_off():
    db = _db()
    on_ts, off_ts, end = _load(db, T0 + 30)
    evs = heater.process(db, end, RIG)
    assert _types(evs) == ["heater_on", "heater_run"]
    on, run = evs
    assert on.ts == on_ts and on.detail["baseline_w"] == 120
    d = run.detail
    assert run.severity == Severity.INFO
    assert d["start_ts"] == on_ts and d["end_ts"] == off_ts
    assert d["duration_min"] == 13.0
    assert abs(d["path_mohm"] - 4.2) < 0.05
    assert abs(d["ah"] - 135 * 13 / 60) < 3
    assert d["alarms"] == [] and "no alarm" in d["summary"]


def test_run_spanning_ticks_is_reported_once():
    db = _db()
    on_ts, off_ts, end = _load(db, T0 + 30)
    evs = []
    for t in range(T0 + 300, end + 300, 300):
        evs += heater.process(db, t, RIG)
    assert _types(evs) == ["heater_on", "heater_run"]
    assert evs[1].detail["duration_min"] == 13.0


def test_run_survives_restart_mid_run():
    """The run in progress is in kv, so a fresh process() call resumes it."""
    db = _db()
    on_ts, off_ts, end = _load(db, T0 + 30)
    first = heater.process(db, on_ts + 300, RIG)
    assert _types(first) == ["heater_on"]
    assert json.loads(db.get_kv(heater.KV_RUN))["start"] == on_ts
    rest = heater.process(db, end, RIG)
    assert _types(rest) == ["heater_run"]
    assert rest[0].detail["start_ts"] == on_ts


def test_low_battery_during_run_is_warn():
    db = _db()
    on_ts, off_ts, end = _load(db, T0 + 30)
    # One raise arrives as two ids on the same second, 20 s before the sample
    # that first catches the heater.
    for t in ("alarm:vebus/276/LowBattery", "alarm:vebus/276/L1/LowBattery"):
        db.insert_event(make(t, Severity.WARN, {}, ts=on_ts - 20))
    db.insert_event(make("alarm:vebus/276/LowBattery_cleared", Severity.INFO, {}, ts=on_ts))
    run = heater.process(db, end, RIG)[-1]
    assert run.severity == Severity.WARN
    assert run.detail["alarms"] == [on_ts - 20]
    assert run.detail["summary"].endswith("LowBattery x1")


def test_small_loads_are_not_runs():
    db = _db()
    _load(db, T0 + 30, w_on=700.0)
    assert heater.process(db, T0 + 7200, RIG) == []


def test_baseline_tracks_a_higher_ordinary_load():
    """+800 W over a 400 W base is a run; the same 1.1 kW over 400 W is not."""
    db = _db()
    t = T0 + 30
    for _ in range(30):
        _sample(db, t, 400.0); t += 30
    for _ in range(4):
        _sample(db, t, 1100.0); t += 30
    for _ in range(4):
        _sample(db, t, 400.0); t += 30
    assert heater.process(db, t, RIG) == []


def test_data_gap_ends_run_as_truncated():
    db = _db()
    t = T0 + 30
    for _ in range(30):
        _sample(db, t, 120.0); t += 30
    on_ts = t
    for _ in range(6):
        _sample(db, t, 1440.0); t += 30
    last = t - 30
    t += 1800                       # collector down for half an hour
    for _ in range(4):
        _sample(db, t, 120.0); t += 30
    evs = heater.process(db, t, RIG)
    assert _types(evs) == ["heater_on", "heater_run"]
    d = evs[1].detail
    assert d["truncated"] and d["end_ts"] == last and d["start_ts"] == on_ts
    assert "data gap" in d["summary"]


def test_no_inverter_dc_means_no_ah_or_path():
    """Runs before PR #13 have no inverter readings: report what is known."""
    db = _db()
    on_ts, off_ts, end = _load(db, T0 + 30)
    db.conn.execute("UPDATE ac_samples SET inverter_dc_voltage = NULL, "
                    "inverter_dc_current = NULL")
    d = heater.process(db, end, RIG)[-1].detail
    assert "ah" not in d and "path_mohm" not in d and "min_inv_v" not in d
    assert d["duration_min"] == 13.0
