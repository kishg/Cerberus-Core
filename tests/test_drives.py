"""Per-drive segmentation, metrics and baseline flags (cerberus.drives).

Synthetic data only. The shapes mirror what the rig produced: the alternator
peaks at 140-220 A, the shore charger sits near 60 A, the joint at instance 24
rests near 30 C, and battery_samples holds one row per pack (bank = sum).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

from cerberus import drives
from cerberus.db import Db
from cerberus.events import Severity

T0 = int(datetime(2026, 10, 5, 9, 0).timestamp())   # after the 09-19 epoch
# This rig's values, which config.yaml sets; the code defaults are neutral.
RIG = drives.Settings(joint_temp_instance=24, pack_count=3, capacity_ah=930.0)
# Same rig, no joint probe configured.
NO_PROBE = drives.Settings(pack_count=3, capacity_ah=930.0)
RIG_EPOCHS = ("2026-09-19",)


def _db() -> Db:
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def _bank(db, ts, amps, volts=13.4, soc=60.0, solar=0.0, shore=False, packs=(1, 2, 3)):
    for pack in packs:
        db.conn.execute(
            "INSERT INTO battery_samples (ts, pack_id, soc, voltage, current) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, pack, soc, volts, amps / len(packs)),
        )
    db.conn.execute(
        "INSERT OR REPLACE INTO solar_samples (ts, charge_current) VALUES (?, ?)",
        (ts, solar),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO ac_samples (ts, input_w, shore_connected) "
        "VALUES (?, ?, ?)",
        (ts, 800.0 if shore else 0.0, 1 if shore else 0),
    )


def _drive(db, start, minutes=40, amps=160.0, volts=13.5, soc0=50.0,
           peak_v=None, joint=None, packs=(1, 2, 3), step_a=45.0):
    """An alternator charge session at 30 s cadence, idle either side."""
    _bank(db, start - 60, -5.0, soc=soc0, packs=packs)
    n = minutes * 2
    for k in range(n):
        ts = start + k * 30
        soc = min(soc0 + k * 0.4, 100.0)
        v = peak_v if (peak_v and k == n - 1) else volts
        # Alternate the current a little so step resistance has steps.
        a = amps + (step_a if k % 2 else 0.0)
        _bank(db, ts, a, volts=v + (0.03 if k % 2 else 0.0), soc=soc, packs=packs)
        if joint is not None:
            db.conn.execute(
                "INSERT INTO temperature_samples (ts, instance, temperature_c) "
                "VALUES (?, 24, ?)",
                (ts, joint + k * 0.3),
            )
    _bank(db, start + n * 30 + 60, -5.0, soc=soc0, packs=packs)
    return start, start + (n - 1) * 30


# --- segmentation -------------------------------------------------------------

def test_alternator_session_is_a_drive():
    db = _db()
    s, e = _drive(db, T0)
    found = drives.find_drives(db, 0, T0 + 86400, RIG)
    assert [(d.start_ts, d.end_ts, d.source) for d in found] == [(s, e, "electrical")]


def test_shore_charger_is_not_a_drive():
    """~60 A for hours looks like charging but never peaks like an alternator."""
    db = _db()
    for k in range(240):
        _bank(db, T0 + k * 30, 60.0)
    assert drives.find_drives(db, 0, T0 + 86400, RIG) == []


def test_shore_flagged_charging_is_excluded_even_at_high_current():
    db = _db()
    for k in range(40):
        _bank(db, T0 + k * 30, 150.0, shore=True)
    assert drives.find_drives(db, 0, T0 + 86400, RIG) == []


def test_solar_is_subtracted():
    """150 A into the bank with 140 A of it from solar is not the alternator."""
    db = _db()
    for k in range(40):
        _bank(db, T0 + k * 30, 150.0, solar=140.0)
    assert drives.find_drives(db, 0, T0 + 86400, RIG) == []


def test_short_gap_is_one_drive_long_gap_is_two():
    db = _db()
    _drive(db, T0, minutes=10)
    _drive(db, T0 + 10 * 60 + 300, minutes=10)       # 5 min stop
    _drive(db, T0 + 3 * 3600, minutes=10)             # hours later
    found = drives.find_drives(db, 0, T0 + 86400, RIG)
    assert len(found) == 2


def test_obd_boundaries_win_over_electrical():
    db = _db()
    _drive(db, T0, minutes=20)
    for k in range(0, 30 * 60, 2):
        db.conn.execute(
            "INSERT INTO engine_samples (ts, rpm) VALUES (?, ?)", (T0 - 120 + k, 1800.0)
        )
    found = drives.find_drives(db, 0, T0 + 86400, RIG)
    assert [d.source for d in found] == ["obd"]
    assert found[0].start_ts == T0 - 120


# --- metrics ------------------------------------------------------------------

def test_metrics_basic_shape():
    db = _db()
    s, e = _drive(db, T0, minutes=40, amps=160.0, joint=29.0)
    m = drives.compute_metrics(db, drives.Drive(s, e, "electrical"), RIG)
    assert m["coverage_pct"] >= 95
    assert 150 <= m["alt_bulk_a"] <= 210
    assert m["alt_ah"] > 100
    # Steps of +-45 A with +-0.03 V: 0.67 mOhm.
    assert abs(m["bank_step_mohm"] - 0.67) < 0.05
    assert "joint_c_per_ka2" in m


def test_joint_ratio_skipped_when_joint_starts_hot():
    """A drive that follows another starts with a hot joint; its rise says
    nothing about the joint itself."""
    db = _db()
    s, e = _drive(db, T0, minutes=40, joint=45.0)
    m = drives.compute_metrics(db, drives.Drive(s, e, "electrical"), RIG)
    assert "joint_c_per_ka2" not in m
    assert m["joint_peak_c"] > 45


def test_joint_metrics_off_without_a_joint_probe():
    """Default settings name no probe, so a van whose instance 24 is something
    else entirely gets no joint figures rather than wrong ones."""
    db = _db()
    s, e = _drive(db, T0, minutes=40, amps=160.0, joint=29.0)
    m = drives.compute_metrics(db, drives.Drive(s, e, "electrical"), NO_PROBE)
    assert not any(k.startswith("joint_") for k in m)


def test_joint_limit_follows_settings():
    d = drives.Drive(T0, T0 + 600, "electrical", {"joint_peak_c": 72.0})
    assert drives.evaluate(d, [], RIG)
    assert not drives.evaluate(d, [], drives.Settings(joint_temp_instance=24,
                                                      joint_limit_c=80.0))


# --- baseline -----------------------------------------------------------------

def _history(n, **over):
    base = {"coverage_pct": 100.0, "alt_bulk_a": 175.0}
    out = []
    for k in range(n):
        h = dict(base)
        h["alt_bulk_a"] = 170.0 + (k % 5) * 2.5
        h.update(over)
        out.append(h)
    return out


def test_anomaly_needs_a_formed_baseline():
    d = drives.Drive(T0, T0 + 3600, "electrical",
                     {"coverage_pct": 100.0, "alt_bulk_a": 120.0})
    assert drives.evaluate(d, _history(drives.MIN_BASELINE - 1)) == []
    flags = drives.evaluate(d, _history(drives.MIN_BASELINE))
    assert [f["metric"] for f in flags] == ["alt_bulk_a"]
    assert flags[0]["z"] < -drives.Z_FLAG


def test_normal_drive_is_not_flagged():
    d = drives.Drive(T0, T0 + 3600, "electrical",
                     {"coverage_pct": 100.0, "alt_bulk_a": 174.0})
    assert drives.evaluate(d, _history(20)) == []


def test_low_coverage_history_is_ignored():
    hist = _history(20, coverage_pct=40.0)
    d = drives.Drive(T0, T0 + 3600, "electrical",
                     {"coverage_pct": 100.0, "alt_bulk_a": 120.0})
    assert drives.evaluate(d, hist) == []


def test_absolute_limits_flag_without_history():
    d = drives.Drive(T0, T0 + 3600, "electrical",
                     {"coverage_pct": 100.0, "peak_bank_v": 14.75,
                      "joint_peak_c": 72.0})
    kinds = {(f["kind"], f["metric"]) for f in drives.evaluate(d, [])}
    assert kinds == {("limit", "peak_bank_v"), ("limit", "joint_peak_c")}


def test_only_new_trouble_codes_flag():
    hist = [{"coverage_pct": 100.0, "dtcs": ["P0562"]}]
    d = drives.Drive(T0, T0 + 3600, "obd",
                     {"coverage_pct": 100.0, "dtcs": ["P0562", "P2463"]})
    flags = drives.evaluate(d, hist)
    assert flags == [{"kind": "dtc", "metric": "dtcs", "value": ["P2463"]}]


def test_epoch_excludes_older_drives():
    db = _db()
    before = int(datetime(2026, 9, 10).timestamp())
    db.conn.execute(
        "INSERT INTO drives (start_ts, end_ts, source, metrics, created_ts) "
        "VALUES (?, ?, 'electrical', ?, 0)",
        (before, before + 600, json.dumps({"alt_bulk_a": 150.0})),
    )
    assert drives.prior_drives(db, T0, epochs=RIG_EPOCHS) == []
    assert len(drives.prior_drives(db, T0, epochs=())) == 1
    assert len(drives.prior_drives(db, T0)) == 1     # no epochs by default


# --- processing -----------------------------------------------------------------

def test_first_run_backfills_silently_then_reports_new_drives(tmp_path):
    db = _db()
    _drive(db, T0)
    now = T0 + 2 * 3600
    assert drives.process(db, now, tmp_path, settings=RIG) == []
    assert db.conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 1

    s2 = T0 + 4 * 3600
    _, e2 = _drive(db, s2)
    # Not yet settled: the engine may only have stopped briefly.
    assert drives.process(db, e2 + 60, tmp_path, settings=RIG) == []
    events = drives.process(db, e2 + drives.SETTLE_S + 60, tmp_path, settings=RIG)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "drive_report" and ev.severity == Severity.INFO
    assert ev.detail["summary"].endswith("— normal")
    assert list(tmp_path.glob("drive-*.md"))


def test_flagged_drive_is_a_warning(tmp_path):
    db = _db()
    drives.process(db, T0 - 3600, tmp_path, settings=RIG)           # first run: nothing yet
    _, e = _drive(db, T0, soc0=90.0, peak_v=14.7)
    events = drives.process(db, e + drives.SETTLE_S + 60, tmp_path, settings=RIG)
    assert len(events) == 1
    assert events[0].severity == Severity.WARN
    assert "peak_bank_v" in events[0].detail["summary"]


def test_drive_is_never_recorded_twice(tmp_path):
    """A rescan whose window starts mid-drive sees a truncated start; overlap
    matching must still recognise it."""
    db = _db()
    drives.process(db, T0 - 3600, tmp_path, settings=RIG)
    s, e = _drive(db, T0, minutes=60)
    drives.process(db, e + drives.SETTLE_S + 60, tmp_path, settings=RIG)
    assert drives.process(db, e + 2 * drives.SETTLE_S, tmp_path, settings=RIG, since=s + 600) == []
    assert db.conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 1


def test_rebuild_recomputes_silently(tmp_path):
    db = _db()
    _drive(db, T0)
    drives.process(db, T0 + 7200, tmp_path, settings=RIG)
    assert drives.rebuild(db, T0 + 7200, tmp_path, settings=RIG) == 1


# --- BMS SoC drift ----------------------------------------------------------------
# 10-21: the BMS sat at 99% while a drive put ~140 Ah into the bank at
# 13.6-13.8 V. Charge that outruns the SoC gain means SoC was reading high.

def _charge(db, start, minutes=50, amps=150.0, volts=13.6, soc=lambda k: 99.0,
            capacity=None):
    """Steady alternator charge with a caller-chosen SoC trace."""
    n = minutes * 2
    for k in range(n):
        ts = start + k * 30
        _bank(db, ts, amps, volts=volts, soc=soc(k))
        if capacity is not None:
            db.conn.execute("UPDATE battery_samples SET capacity_ah = ? WHERE ts = ?",
                            (capacity, ts))
    return drives.Drive(start, start + (n - 1) * 30, "electrical")


def test_pinned_soc_under_heavy_charge_is_flagged():
    db = _db()
    d = _charge(db, T0)                       # ~123 Ah at a flat 99%
    d.metrics = drives.compute_metrics(db, d, RIG)
    assert 120 <= d.metrics["bank_ah"] <= 126
    assert abs(d.metrics["soc_drift_pct"] - 13.2) < 0.5
    # Not a full bank, whatever the BMS says: no full-bank peak voltage.
    assert "peak_bank_v_full" not in d.metrics
    d.flags = drives.evaluate(d, [])
    assert [f["kind"] for f in d.flags] == ["soc_drift"]
    assert "BMS SoC reads ~13 pts high" in drives.summary_line(d)
    assert "BMS SoC reads high" in drives.render_report(d, [])


def test_honest_soc_is_not_flagged():
    db = _db()
    # 150 A for 30 s into 930 Ah is 0.134 points per sample.
    d = _charge(db, T0, soc=lambda k: 70.0 + k * 0.134)
    d.metrics = drives.compute_metrics(db, d, RIG)
    assert abs(d.metrics["soc_drift_pct"]) < 1.0
    assert drives.evaluate(d, []) == []


def test_absorption_into_a_full_bank_is_not_drift():
    """At 14.4 V the bank really is full and SoC cannot rise."""
    db = _db()
    d = _charge(db, T0, amps=60.0, volts=14.4, soc=lambda k: 100.0)
    d.metrics = drives.compute_metrics(db, d, RIG)
    assert d.metrics["bank_ah"] == 0
    assert "soc_drift_pct" not in d.metrics
    assert d.metrics["peak_bank_v_full"] == 14.4


def test_drift_uses_reported_pack_capacity():
    db = _db()
    d = _charge(db, T0, capacity=100.0)       # 3 x 100 Ah: 123 Ah is 41 points
    m = drives.compute_metrics(db, d, RIG)
    assert abs(m["soc_drift_pct"] - 41.0) < 1.5


# --- thresholds relative to the van (step 3b) ----------------------------------

def test_rig_thresholds_are_the_old_constants():
    s = drives.Settings(engine_charge_a=200.0)
    assert (s.charge_on_a, s.drive_peak_a, s.step_min_a) == (20.0, 100.0, 40.0)


def test_charge_on_has_a_floor():
    assert drives.Settings(engine_charge_a=20.0).charge_on_a == 5.0


def test_dc_dc_drive_is_found_only_with_its_own_rating():
    """A 30 A DC-DC never reaches the 100 A peak a direct 200 A alternator
    needs, so on the old fixed thresholds this van had no drives at all."""
    db = _db()
    _drive(db, T0, amps=26.0, step_a=2.0, packs=(1,))
    assert drives.find_drives(db, 0, T0 + 86400, drives.Settings(pack_count=1)) == []
    dcdc = drives.Settings(pack_count=1, engine_charge_a=30.0)
    found = drives.find_drives(db, 0, T0 + 86400, dcdc)
    assert len(found) == 1 and found[0].source == "electrical"


def test_single_monitor_bank_needs_pack_count_one():
    """battery_samples from a shunt has one row per timestamp; requiring
    three (the old hardcoded count) silently found nothing."""
    db = _db()
    _drive(db, T0, packs=(1,))
    assert drives.find_drives(db, 0, T0 + 86400, RIG) == []
    assert len(drives.find_drives(db, 0, T0 + 86400, drives.Settings(pack_count=1))) == 1


def test_no_capacity_anywhere_skips_soc_drift():
    db = _db()
    d = _charge(db, T0)
    m = drives.compute_metrics(db, d, drives.Settings(pack_count=3))
    assert "bank_ah" in m and "soc_drift_pct" not in m


def test_configured_capacity_is_used_when_the_bms_is_silent():
    db = _db()
    d = _charge(db, T0)
    m300 = drives.compute_metrics(db, d, drives.Settings(pack_count=3, capacity_ah=300.0))
    m930 = drives.compute_metrics(db, d, RIG)
    assert m300["soc_drift_pct"] > m930["soc_drift_pct"]


def test_obd_drive_reports_odometer_miles_and_coolant():
    """A drive with the OBD adapter: odometer gives the miles, coolant its
    start, end and peak, and the summary line uses both."""
    db = _db()
    s, e = _drive(db, T0, minutes=20)
    for k in range(0, 20 * 60, 2):
        coolant = min(40.0 + k / 10, 88.0)
        db.conn.execute("INSERT INTO engine_samples (ts, rpm, speed_mph, coolant_c) "
                        "VALUES (?, ?, ?, ?)", (T0 + k, 1800.0, 50.0, coolant))
    for k in range(0, 20 * 60, 60):
        db.conn.execute("INSERT INTO obd_samples (ts, ecu, name, raw, value) "
                        "VALUES (?, '7DF', 'odometer_mi', '', ?)", (T0 + k, 36960.0 + k / 72))
    d = drives.find_drives(db, 0, T0 + 86400, RIG)[0]
    d.metrics = drives.compute_metrics(db, d, RIG)
    m = d.metrics
    assert m["odometer_start_mi"] == 36960.0
    assert m["odometer_driven_mi"] == 15.8
    assert (m["coolant_start_c"], m["coolant_max_c"]) == (40.0, 88.0)
    line = drives.summary_line(d)
    assert "16 mi" in line and "coolant max 88 °C" in line


def test_electrical_drive_has_no_odometer_or_coolant():
    db = _db()
    s, e = _drive(db, T0, minutes=20)
    m = drives.compute_metrics(db, drives.Drive(s, e, "electrical"), RIG)
    assert not any(k.startswith(("odometer", "coolant")) for k in m)
