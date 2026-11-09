"""Step 5.1: the live snapshot, the UI's JSON views, and its server."""

from __future__ import annotations

import json
import sqlite3
import textwrap
import threading
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cerberus import config as cfg_mod
from cerberus import live
from cerberus.ac import AcState
from cerberus.db import Db
from cerberus.detectors import KV_LAST_FULL_CHARGE
from cerberus.model import BankState, PackState
from cerberus.solar import SolarState
from cerberus.ui import api
from cerberus.ui.server import App, connect_ro, serve

NOW = 1793470000
NY = ZoneInfo("America/New_York")


def _pack(i, soc=90.0, v=13.3, a=-2.0, cap=None):
    return PackState(pack_id=i, uc=f"uc{i}", instance=i, soc=soc, voltage=v, current=a,
                     power=v * a, temperature=21.0, timetogo=None, ts=NOW, capacity_ah=cap)


def _bank(*packs):
    return BankState(ts=NOW, packs={p.pack_id: p for p in packs})


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "c.db"))


# --- snapshot --------------------------------------------------------------------

def test_snapshot_bank_is_mean_soc_and_summed_current():
    s = live.snapshot(NOW, True, _bank(_pack(1, 99, a=-1), _pack(2, 98, a=-1), _pack(3, 97, a=-1)),
                      SolarState(), AcState(), capacity_fallback_ah=930)
    b = s["bank"]
    assert (b["soc"], b["current"], b["capacity_ah"]) == (98.0, -3.0, 930)
    assert len(b["packs"]) == 3 and s["solar"] is None and s["ac"] is None


def test_snapshot_carries_the_inverter_dc_current():
    ac = AcState()
    ac.dc_current, ac.last_update_ts = -9.0, NOW
    ac.inverter_state = 9
    s = live.snapshot(NOW, True, None, SolarState(), ac)
    assert s["ac"] is not None and s["ac"]["dc_current"] == -9.0


def test_snapshot_uses_reported_capacity_only_when_every_pack_reports():
    full = live.snapshot(NOW, True, _bank(_pack(1, cap=310), _pack(2, cap=310)),
                         SolarState(), AcState(), capacity_fallback_ah=999)
    partial = live.snapshot(NOW, True, _bank(_pack(1, cap=310), _pack(2)),
                            SolarState(), AcState(), capacity_fallback_ah=999)
    assert full["bank"]["capacity_ah"] == 620 and partial["bank"]["capacity_ah"] == 999


def test_snapshot_without_a_bank_is_null_not_zero():
    s = live.snapshot(NOW, False, None, SolarState(), AcState())
    assert s["bank"] is None and s["cerbo_online"] is False


# --- now view --------------------------------------------------------------------

def _write_snapshot(db, ts, bank=True):
    b = BankState(ts=ts, packs={1: _pack(1, 80, a=-10)}) if bank else None
    snap = live.snapshot(ts, True, b, SolarState(), AcState(), capacity_fallback_ah=310)
    db.set_kv(live.KV_SNAPSHOT, live.dumps(snap))


def test_no_snapshot_means_collector_not_running(db):
    v = api.now_view(db.conn, api.Context(), NOW)
    assert v["collector_up"] is False and v["live"] is False and v["snapshot"] is None


def test_fresh_snapshot_is_live_and_gives_time_to_empty(db):
    _write_snapshot(db, NOW - 3)
    v = api.now_view(db.conn, api.Context(), NOW)
    assert v["collector_up"] and v["cerbo_online"] and v["live"]
    assert v["time_to_empty_h"] == 24.8          # 80% of 310 Ah at 10 A


def test_stale_snapshot_is_collector_down(db):
    _write_snapshot(db, NOW - api.SNAPSHOT_STALE_S - 1)
    v = api.now_view(db.conn, api.Context(), NOW)
    assert v["collector_up"] is False and v["cerbo_online"] is False


def _dc_snap(bank_a=-3.0, solar_a=None, ac=None):
    return {"bank": {"ts": NOW, "current": bank_a, "voltage": 13.3},
            "solar": None if solar_a is None else {"charge_a": solar_a}, "ac": ac}


@pytest.mark.parametrize("snap, engine, out", [
    # Parked, inverter off: the bank's draw is all DC load.
    (_dc_snap(-3.0, ac={"inverter_state": "off", "dc_current": None}), False,
     {"a": 3.0, "w": 40, "ts": NOW}),
    # Solar covers some of it.
    (_dc_snap(5.0, solar_a=8.0), False, {"a": 3.0, "w": 40, "ts": NOW}),
    # The inverter's draw is AC load, not DC.
    (_dc_snap(-12.0, ac={"inverter_state": "inverting", "dc_current": -10.0}), False,
     {"a": 2.0, "w": 27, "ts": NOW}),
    # Pack rounding just under zero reads as zero.
    (_dc_snap(0.25), False, {"a": 0.0, "w": 0, "ts": NOW}),
    # An unseen charger, or the alternator, would read as load.
    (_dc_snap(20.0), False, {"a": None, "reason": "unmeasured"}),
    (_dc_snap(40.0), True, {"a": None, "reason": "engine"}),
    (_dc_snap(-12.0, ac={"inverter_state": "inverting", "dc_current": None}), False,
     {"a": None, "reason": "inverter"}),
    ({"bank": None}, False, None),
])
def test_dc_loads(snap, engine, out):
    assert api.dc_loads(snap, engine) == out


def test_engine_charging_follows_the_acr_and_obd(db):
    assert not api.engine_charging(db.conn, NOW)
    db.conn.execute("INSERT INTO acr_samples (ts, field1, raw) VALUES (?, 3, '')", (NOW - 5,))
    assert not api.engine_charging(db.conn, NOW)                 # idle
    db.conn.execute("INSERT INTO acr_samples (ts, field1, raw) VALUES (?, 2, '')", (NOW - 2,))
    assert api.engine_charging(db.conn, NOW)                     # connected
    assert not api.engine_charging(db.conn, NOW + api.LIVE_STALE_S + 5)


def test_history_works_out_dc_loads_and_skips_engine_minutes(db):
    t0 = (NOW // 3600) * 3600 - 3600
    for k in range(4):                                  # four minutes, 3 packs
        ts = t0 + k * 60
        for p in (1, 2, 3):
            db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current) "
                            "VALUES (?, ?, 90, 13.0, ?)", (ts, p, -1.0 if k < 3 else 15.0))
        db.conn.execute("INSERT INTO solar_samples (ts, charge_current) VALUES (?, 2.0)", (ts,))
    # Minute 2: the engine turning (any bank figure then is untrustworthy).
    db.conn.execute("INSERT INTO acr_samples (ts, field1, raw) VALUES (?, 2, '')", (t0 + 120,))
    ctx = api.Context(pack_count=3)
    rows = api.history_view(db.conn, ctx, "24h", NOW)["dc_w"]
    # Minutes 0-1: 2 A solar + 3 A from the bank = 5 A at 13 V. Minute 2 is the
    # engine; minute 3 (bank +15 A, solar 2 A) is an unseen charger. Both out.
    assert sum(v for _, v in rows) == 65


def test_now_view_gives_dc_loads_while_live(db):
    _write_snapshot(db, NOW - 3)
    assert api.now_view(db.conn, api.Context(), NOW)["dc_loads"]["a"] == 10.0
    assert api.now_view(db.conn, api.Context(), NOW + api.LIVE_STALE_S + 60)["dc_loads"] is None


@pytest.mark.parametrize("days, trusted", [(None, False), (2, True), (api.SOC_TRUST_DAYS + 1, False)])
def test_soc_trust(days, trusted):
    t = api.soc_trust(None if days is None else NOW - days * 86400, NOW)
    assert t["trusted"] is trusted and "full charge" in t["note"]


def test_soc_trust_reads_the_full_charge_detector(db):
    db.set_kv(KV_LAST_FULL_CHARGE, str(NOW - 86400))
    assert api.now_view(db.conn, api.Context(), NOW)["soc_trust"]["trusted"]


@pytest.mark.parametrize("soc, amps, cap, out", [
    (50, -5.0, 200, 20.0), (50, 3.0, 200, None), (50, -0.2, 200, None), (50, -5.0, None, None)])
def test_time_to_empty(soc, amps, cap, out):
    assert api.time_to_empty_h(soc, amps, cap) == out


def test_now_lists_open_findings_only(db):
    db.set_kv(KV_LAST_FULL_CHARGE, str(NOW - 86400))
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, ?, ?, ?)",
                    (NOW - 60, "solar_output_open", "warn",
                     json.dumps({"detail": {"pv_voltage": 20.5}})))
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, ?, ?, ?)",
                    (NOW - 86400, "temp_high", "warn", json.dumps({"detail": {"pack": 1}})))
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, ?, ?, ?)",
                    (NOW - 86000, "temp_high_cleared", "info", json.dumps({"detail": {"pack": 1}})))
    found = api.now_view(db.conn, api.Context(), NOW)["findings"]
    assert [(f["key"], f["severity"]) for f in found] == [("solar_output_open", "urgent")]
    full = api.findings_view(db.conn, api.Context(), NOW)["findings"]
    assert {f["key"]: f["status"] for f in full} == {"solar_output_open": "open", "battery_hot": "resolved"}


def test_views_carry_the_temperature_unit(db):
    assert api.now_view(db.conn, api.Context(), NOW)["temp_unit"] == "F"
    ctx = api.Context(temp_unit="C")
    assert api.now_view(db.conn, ctx, NOW)["temp_unit"] == "C"
    assert api.findings_view(db.conn, ctx, NOW)["temp_unit"] == "C"
    assert ctx.findings_settings().temp_unit == "C"


# --- history ---------------------------------------------------------------------

def _samples(db, ts, packs=(1, 2, 3), soc=90.0, amps=-3.0):
    for p in packs:
        db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, voltage, current) "
                        "VALUES (?, ?, ?, 13.3, ?)", (ts, p, soc, amps / len(packs)))


def test_history_counts_only_whole_bank_timestamps(db):
    _samples(db, NOW - 600)
    _samples(db, NOW - 300, packs=(1, 2))          # a pack missing: excluded
    h = api.history_view(db.conn, api.Context(pack_count=3), "24h", NOW)
    assert len(h["bank"]) == 1 and h["bank"][0][3] == -3.0


def test_history_buckets_and_rejects_unknown_ranges(db):
    for k in range(12):                            # an hour of 5-min data
        _samples(db, NOW - 3600 + k * 300, soc=50 + k)
    h = api.history_view(db.conn, api.Context(pack_count=3), "7d", NOW)
    assert h["bucket_s"] == 1800 and len(h["bank"]) <= 3
    with pytest.raises(ValueError):
        api.history_view(db.conn, api.Context(), "1y", NOW)


def test_day_buckets_start_at_van_local_midnight(db):
    """Not UTC's: a UTC day labelled in New York time runs 8 PM to 8 PM."""
    midnight = int(datetime(2026, 10, 30, 0, 0, tzinfo=NY).timestamp())
    _samples(db, midnight + 3600)                  # 01:00 local, 10-30
    _samples(db, midnight - 3600)                  # 23:00 local, 10-29
    h = api.history_view(db.conn, api.Context(pack_count=3, tz=NY), "all", midnight + 86400)
    assert [r[0] for r in h["bank"]] == [midnight - 86400, midnight]


def test_daily_yield_is_per_van_local_day(db):
    # 23:30 New York on 10-30 is 03:30 UTC on 10-31: it belongs to 10-30.
    late = int(datetime(2026, 10, 30, 23, 30, tzinfo=NY).timestamp())
    db.conn.execute("INSERT INTO solar_samples (ts, yield_today_kwh) VALUES (?, 0.73)", (late,))
    assert api.daily_yield(db.conn, api.Context(tz=NY), 0) == [["2026-10-30", 0.73]]


# --- server ----------------------------------------------------------------------

@pytest.fixture
def server(db):
    _write_snapshot(db, int(datetime.now().timestamp()))
    httpd = serve(App(db._path, 1, 310, None), "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_server_serves_the_app_and_its_api(server):
    code, ctype, body = _get(server + "/")
    assert code == 200 and ctype.startswith("text/html") and b"Cerberus" in body
    assert _get(server + "/app.js")[0] == 200
    code, ctype, body = _get(server + "/api/now")
    assert code == 200 and json.loads(body)["collector_up"] is True
    assert json.loads(_get(server + "/api/history?range=7d")[2])["range"] == "7d"
    assert "findings" in json.loads(_get(server + "/api/findings")[2])
    assert "tyres" in json.loads(_get(server + "/api/v1/chassis")[2])
    assert json.loads(_get(server + "/api/about")[2])["license"] == "AGPL-3.0-or-later"
    h = json.loads(_get(server + "/api/health")[2])
    assert {"sources", "coverage", "episodes", "missing_s"} <= set(h)


@pytest.mark.parametrize("path, code", [
    ("/api/history?range=forever", 400),
    ("/api/nope", 404),
    ("/../config.yaml", 404),
    ("/%2e%2e/%2e%2e/config.yaml", 404),
    ("/static/app.js", 404),
])
def test_server_rejects_everything_else(server, path, code):
    assert _get(server + path)[0] == code


def test_ui_connection_cannot_write(db):
    conn = connect_ro(db._path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")


# --- config ------------------------------------------------------------------------

def test_load_ui_reads_bank_and_ui_sections(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
        storage: {sqlite_path: /data/x.db}
        packs: {uc1: 1, uc2: 2}
        bank: {capacity_ah: 620}
        ui: {port: 9000}
    """))
    u = cfg_mod.load_ui(p)
    assert (u.port, u.host, u.pack_count, u.capacity_ah, u.sqlite_path) == (9000, "0.0.0.0", 2, 620.0, "/data/x.db")


def test_load_ui_rejects_a_bad_port(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("ui: {port: 70000}\n")
    with pytest.raises(cfg_mod.ConfigError, match="ui.port"):
        cfg_mod.load_ui(p)


# --- vehicle (ACR + OBD) ---------------------------------------------------------

def _obd(db, ts, name, value):
    db.conn.execute("INSERT INTO obd_samples (ts, ecu, name, raw, value) VALUES (?, '7DF', ?, '', ?)",
                    (ts, name, value))


def test_no_vehicle_section_without_acr_or_obd(db):
    assert api.chassis_view(db.conn, api.Context(), NOW)["vehicle"] is None


def test_vehicle_idling_with_chassis_fuel_and_def(db):
    db.insert_acr_sample(ts=NOW - 5, field1=2, chassis_v_cv=1421, house_v_cv=1418, field7=1, raw="")
    _obd(db, NOW - 2, "rpm", 768.0)
    _obd(db, NOW - 2, "speed_mph", 0.0)
    _obd(db, NOW - 60, "fuel_level_pct", 64.7)
    _obd(db, NOW - 60, "def_level_pct", 78.4)
    _obd(db, NOW - 60, "odometer_mi", 36960.4)
    v = api.chassis_view(db.conn, api.Context(), NOW)["vehicle"]
    assert v["chassis"] == {"ts": NOW - 5, "voltage": 14.21, "linked": True}
    assert v["engine"]["state"] == "idling" and v["engine"]["rpm"] == 768.0
    assert v["fuel"]["pct"] == 65 and v["def"]["pct"] == 78
    assert v["odometer"] == {"ts": NOW - 60, "mi": 36960, "source": "obd"}


def test_vehicle_engine_off_keeps_last_fuel_and_drops_stale_chassis(db):
    db.insert_acr_sample(ts=NOW - 3600, field1=0, chassis_v_cv=1290, house_v_cv=1330, field7=0, raw="")
    _obd(db, NOW - 1800, "rpm", 1500.0)
    _obd(db, NOW - 1800, "speed_mph", 45.0)
    _obd(db, NOW - 1800, "fuel_level_pct", 50.0)
    v = api.chassis_view(db.conn, api.Context(), NOW)["vehicle"]
    assert "chassis" not in v
    assert v["engine"] == {"ts": NOW - 1800, "state": "off", "rpm": None, "speed_mph": None}
    assert v["fuel"] == {"ts": NOW - 1800, "pct": 50, "source": "obd"}


def test_vehicle_driving(db):
    _obd(db, NOW - 1, "rpm", 1800.0)
    _obd(db, NOW - 1, "speed_mph", 52.0)
    assert api.chassis_view(db.conn, api.Context(), NOW)["vehicle"]["engine"]["state"] == "driving"



def test_now_has_no_vehicle_section_any_more(db):
    """Chassis data moved to its own tab (2026-11-01)."""
    assert "vehicle" not in api.now_view(db.conn, api.Context(), NOW)


def test_chassis_view_with_import_findings_and_drives(db):
    db.insert_vehicle_samples(NOW - 600, "mercedes_me", {
        "tire_fl_psi": 55, "tire_fr_psi": 55, "tire_rl_psi": 65, "tire_rr_psi": 66,
        "tire_warning": 0, "starter_battery_state": 3, "coolant_low": 0, "lock_state": 2,
        "service_due_mi": 590})
    db.conn.execute("INSERT INTO drives (start_ts, end_ts, source, metrics, flags, created_ts) "
                    "VALUES (?, ?, 'obd', ?, '[]', 0)",
                    (NOW - 7200, NOW - 4800, '{"odometer_driven_mi": 31.2, "alt_ah": 62, "coolant_max_c": 88}'))
    c = api.chassis_view(db.conn, api.Context(), NOW)
    assert c["tyres"]["rr"]["value"] == 66 and c["tyres"]["warning"] is False
    assert c["starter_battery"]["label"] == "weak"
    assert c["warnings"]["coolant_low"]["on"] is False
    assert c["lock_state"]["value"] == 2
    assert c["service_due"] == {"ts": NOW - 600, "value": 590}    # tile, though no finding yet
    assert [f["key"] for f in c["findings"]] == ["starter_battery"]
    assert c["drives"][0]["miles"] == 31.2 and c["drives"][0]["coolant_max_c"] == 88


def test_chassis_view_leaves_out_warnings_that_stopped_arriving(db):
    db.insert_vehicle_samples(NOW - 3 * 86400, "mercedes_me", {"engine_light": 0, "coolant_low": 0})
    db.insert_vehicle_samples(NOW - 600, "mercedes_me", {"coolant_low": 0})
    assert list(api.chassis_view(db.conn, api.Context(), NOW)["warnings"]) == ["coolant_low"]


def test_chassis_view_marks_the_low_tyre(db):
    for i in range(4):                       # four drives; the last ends 10 psi down
        start, psi = NOW - 3600 * (8 - 2 * i), 45 if i == 3 else 55
        db.insert_vehicle_samples(start, "mercedes_me", {"ignition_state": 4})
        db.insert_vehicle_samples(start + 1800, "mercedes_me",
                                  {"ignition_state": 4, "tire_fl_psi": psi, "tire_rr_psi": 66})
        db.insert_vehicle_samples(start + 1860, "mercedes_me", {"ignition_state": 0})
    t = api.chassis_view(db.conn, api.Context(), NOW)["tyres"]
    assert t["fl"]["value"] == 45 and t["fl"]["low_from"] == 55
    assert "low_from" not in t["rr"] and t["fr"] is None


def test_chassis_view_empty_van(db):
    c = api.chassis_view(db.conn, api.Context(), NOW)
    assert c["vehicle"] is None and c["tyres"] is None and c["drives"] == []
    assert c["service_due"] is None
