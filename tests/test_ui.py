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
