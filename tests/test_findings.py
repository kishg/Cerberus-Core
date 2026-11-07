"""Step 5.2: findings derived from measured rows and edge events."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cerberus import findings as F
from cerberus.db import Db

NY = ZoneInfo("America/New_York")
NOW = int(datetime(2026, 10, 31, 15, 0, tzinfo=NY).timestamp())
DAY = 86400
S = F.Settings(tz=NY)


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "f.db"))


def _keys(db, now=NOW):
    return {f.key: f for f in F.derive(db.conn, S, now)}


def _solar_day(db, day, **metrics):
    db.conn.execute("INSERT INTO solar_days (day, metrics, flags, created_ts) VALUES (?, ?, '[]', 0)",
                    (day, json.dumps(metrics)))


def _event(db, ts, typ, sev="warn", **detail):
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, ?, ?, ?)",
                    (ts, typ, sev, json.dumps({"detail": detail})))


def _drive(db, ts, flags, **metrics):
    db.conn.execute("INSERT INTO drives (start_ts, end_ts, source, metrics, flags, created_ts) "
                    "VALUES (?, ?, 'electrical', ?, ?, 0)",
                    (ts, ts + 3600, json.dumps(metrics), json.dumps(flags)))


def test_empty_history_has_no_findings_but_no_full_charge(db):
    assert list(_keys(db)) == ["no_full_charge"]


# --- solar path ------------------------------------------------------------------

def test_solar_path_open_since_the_start_of_the_current_run(db):
    """This van, 10-21 .. 10-30: crossed 75 mOhm on 10-27 and stayed above."""
    for day, v in (("2026-10-21", 64.7), ("2026-10-24", 69.4), ("2026-10-26", 74.5),
                   ("2026-10-27", 78.7), ("2026-10-28", 79.5), ("2026-10-30", 92.4)):
        _solar_day(db, day, path_mohm_at_absorption=v)
    f = _keys(db)["solar_path_resistance"]
    assert (f.status, f.severity) == ("open", "attention")
    assert "about 92 mΩ on Oct 30" in f.explain and "since Oct 27" in f.explain
    assert "lowest reading in the last 30 days was 65" in f.explain
    assert f.evidence["limit"] == 75.0 and len(f.evidence["points"]) == 6


def test_solar_path_resolves_and_then_ages_out(db):
    _solar_day(db, "2026-10-20", path_mohm_at_absorption=90.0)
    _solar_day(db, "2026-10-28", path_mohm_at_absorption=50.0)
    assert _keys(db)["solar_path_resistance"].status == "resolved"
    assert "solar_path_resistance" not in _keys(db, NOW + 20 * DAY)


def test_solar_path_falls_back_to_the_measured_path(db):
    _solar_day(db, "2026-10-30", path_mohm=80.0)
    assert _keys(db)["solar_path_resistance"].status == "open"


# --- early float ---------------------------------------------------------------------

def test_early_float_resolved_once_it_floats_full_again(db):
    for day, soc in (("2026-10-23", 79.0), ("2026-10-24", 62.0), ("2026-10-25", 91.7),
                     ("2026-10-26", 99.0), ("2026-10-30", 99.0)):
        _solar_day(db, day, soc_at_float=soc)
    f = _keys(db)["early_float"]
    assert (f.status, f.severity) == ("resolved", "info")
    assert "3 of the last 5 days" in f.explain and "62%–92%" in f.explain


# --- SoC honesty ---------------------------------------------------------------------

def test_soc_reads_high_until_a_full_charge_resets_it(db):
    drive = NOW - 6 * DAY
    _drive(db, drive, [{"kind": "soc_drift", "metric": "soc_drift_pct", "value": 5.4,
                        "bank_ah": 102.5, "soc_gain": 5.6}])
    db.set_kv("last_full_charge_ts", str(drive - DAY))
    f = _keys(db)["soc_reads_high"]
    assert f.status == "open" and "about 102 Ah" in f.explain and "about 5 points higher" in f.explain
    db.set_kv("last_full_charge_ts", str(drive + 3 * 3600))
    assert _keys(db)["soc_reads_high"].status == "resolved"


def test_no_full_charge_after_the_reminder_period(db):
    db.set_kv("last_full_charge_ts", str(NOW - 6 * DAY))
    assert "no_full_charge" not in _keys(db)
    db.set_kv("last_full_charge_ts", str(NOW - 45 * DAY))
    assert "45 days ago" in _keys(db)["no_full_charge"].explain


# --- drives ----------------------------------------------------------------------------

def test_one_overshoot_reads_as_one(db):
    _drive(db, NOW - 6 * DAY, [{"kind": "limit", "metric": "peak_bank_v", "value": 14.62,
                                "limit": ">= 14.6 V"}])
    f = _keys(db)["alternator_overshoot"]
    assert f.status == "open" and f.explain.startswith("On Oct 25 the bank reached 14.62 V")


def test_overshoot_resolves_after_five_clean_drives(db):
    _drive(db, NOW - 12 * DAY, [{"kind": "limit", "metric": "peak_bank_v", "value": 14.7,
                                 "limit": ">= 14.6 V"}])
    for k in range(5):
        _drive(db, NOW - (10 - k) * DAY, [])
    assert _keys(db)["alternator_overshoot"].status == "resolved"


# --- edge events --------------------------------------------------------------------------

def test_edge_finding_is_open_until_cleared(db):
    _event(db, NOW - 3600, "solar_output_open", pv_voltage=20.7)
    f = _keys(db)["solar_output_open"]
    assert (f.status, f.severity) == ("open", "urgent") and "PV at 21 V" in f.explain
    _event(db, NOW - 600, "solar_output_open_cleared", "info")
    f = _keys(db)["solar_output_open"]
    assert (f.status, f.severity) == ("resolved", "info") and "Cleared Oct 31" in f.explain


def test_the_zero_volt_glitch_is_not_a_finding(db):
    """2026-10-29 17:24: a missing pack voltage parsed as 0.0 raised a CRIT."""
    _event(db, NOW - 2 * DAY, "low_soc_crit", "crit", min_soc=99.0, min_voltage=0.0)
    _event(db, NOW - 2 * DAY + 5, "low_soc_crit_cleared", "info", min_voltage=13.3)
    assert "critical_voltage" not in _keys(db)


def test_inverter_alarm_counts_distinct_times_not_both_reports(db):
    """The inverter reports each alarm twice, as total and as phase L1."""
    for k in range(3):
        t = NOW - (k + 1) * DAY
        for typ in ("alarm:vebus/276/LowBattery", "alarm:vebus/276/L1/LowBattery"):
            _event(db, t, typ, alarm=typ[6:], level=1)
            _event(db, t + 20, typ + "_cleared", "info", alarm=typ[6:], level=0)
    f = _keys(db)["inverter_low_battery"]
    assert f.status == "resolved" and "3 times in the last 30 days" in f.explain
    # Its span is the window it talks about, not the first alarm ever.
    _event(db, NOW - 200 * DAY, "alarm:vebus/276/LowBattery", alarm="vebus/276/LowBattery")
    _event(db, NOW - 200 * DAY + 20, "alarm:vebus/276/LowBattery_cleared", "info")
    assert _keys(db)["inverter_low_battery"].first_seen == NOW - 3 * DAY


def test_old_resolved_findings_drop_off(db):
    _event(db, NOW - 40 * DAY, "temp_high", pack=1, temperature=47.0)
    _event(db, NOW - 40 * DAY + 600, "temp_high_cleared", "info", pack=1, temperature=40.0)
    assert "battery_hot" not in _keys(db)


def test_open_findings_sort_first_by_severity(db):
    _event(db, NOW - 3600, "cell_imbalance_bank", spread_mv=140, cell_min_mv=3410, cell_max_mv=3550)
    _event(db, NOW - 7200, "solar_output_open", pv_voltage=20.0)
    db.set_kv("last_full_charge_ts", str(NOW - DAY))
    order = [(f.status, f.severity, f.key) for f in F.derive(db.conn, S, NOW)]
    assert order[:2] == [("open", "urgent", "solar_output_open"),
                         ("open", "attention", "cell_imbalance")]


# --- engine (OBD) ----------------------------------------------------------------

def _obd(db, ts, name, value):
    db.conn.execute("INSERT INTO obd_samples (ts, ecu, name, raw, value) VALUES (?, '7DF', ?, '', ?)",
                    (ts, name, value))


def test_engine_faults_open_while_the_latest_read_has_codes(db):
    db.insert_dtcs(NOW - 2 * DAY, "03", [], "4300")
    db.insert_dtcs(NOW - DAY, "03", ["P2002", "P0401"], "raw")
    db.insert_dtcs(NOW - DAY, "07", ["P0299"], "raw")
    f = _keys(db)["engine_faults"]
    assert (f.status, f.severity) == ("open", "attention")
    assert "P2002, P0401" in f.explain and "Pending (not yet confirmed): P0299" in f.explain
    assert "never clears" in f.action


def test_engine_faults_resolve_when_a_later_read_is_clean(db):
    db.insert_dtcs(NOW - 3 * DAY, "03", ["P2002"], "raw")
    db.insert_dtcs(NOW - DAY, "03", [], "4300")
    f = _keys(db)["engine_faults"]
    assert (f.status, f.severity) == ("resolved", "info")
    assert "None stored at the read on Oct 30" in f.explain


def test_no_engine_finding_without_codes_or_an_adapter(db):
    assert "engine_faults" not in _keys(db)
    db.insert_dtcs(NOW - DAY, "03", [], "4300")
    assert "engine_faults" not in _keys(db)


def test_def_low_opens_under_the_threshold_and_resolves_after_a_refill(db):
    _obd(db, NOW - 3 * DAY, "def_level_pct", 40.0)
    assert "def_low" not in _keys(db)
    _obd(db, NOW - 2 * DAY, "def_level_pct", 12.2)
    f = _keys(db)["def_low"]
    assert f.status == "open" and "read 12% on Oct 29" in f.explain
    _obd(db, NOW - DAY, "def_level_pct", 95.0)
    f = _keys(db)["def_low"]
    assert f.status == "resolved" and "It read 95% on Oct 30" in f.explain


def test_def_low_uses_the_configured_level_and_the_imported_reading(db):
    db.insert_vehicle_samples(NOW - DAY, "mercedes_me", {"def_level_pct": 45.0})
    assert "def_low" not in _keys(db)                       # default 20%
    high = F.Settings(tz=NY, def_low_pct=50.0)
    f = {f.key: f for f in F.derive(db.conn, high, NOW)}["def_low"]
    assert f.status == "open" and "read 45% on Oct 30, under the 50% alert level" in f.explain
    _obd(db, NOW - 2 * DAY, "def_level_pct", 90.0)          # older than the import
    assert {f.key: f for f in F.derive(db.conn, high, NOW)}["def_low"].status == "open"
