"""Opt-in box health reports (cerberus.healthreport, Core #42).

What this proves:
- Off by default: nothing is sent, and no thread starts.
- A report is box health only: the payload's fields come from an allowlist,
  with no readings and no position, even when the database holds both.
- Delivery: bearer token, the last attempt kept for Settings, `since_last`
  from the last report that got through, failures recorded and retried.
- Config: https only (http for localhost), a target when on, the token from
  the environment, a floor on the interval.
- The UI shows the last report exactly, and never the token.
"""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import healthreport as hr
from cerberus.config import HealthReportConfig
from cerberus.db import Db
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
NOW = 1_800_000_000
DEVICE = "3f0c8a52-6d1e-4b7a-9a43-0d2a6c1e9b11"

# Every field a report may carry. Anything else is a leak.
ALLOWED = {
    "format", "version", "box", "box.id", "box.core_version", "sent_at", "interval_s",
    "uptime", "uptime.collector_s", "uptime.host_s",
    "since_last", "since_last.last_ok_at", "since_last.silent_s",
    "since_last.collector_restarts", "since_last.collector_down_s",
    "system", "system.disk", "system.disk.total_bytes", "system.disk.free_bytes",
    "system.memory", "system.memory.total_bytes", "system.memory.available_bytes",
    "system.cpu_temp_c",
    "sources", "sources.key", "sources.status", "sources.age_s",
    "coverage_today_pct",
}


def _paths(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            yield p
            yield from _paths(v, p)
    elif isinstance(obj, list):
        for v in obj:
            yield from _paths(v, prefix)


@pytest.fixture(autouse=True)
def _cerbo_password(monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "c.db"))
    yield d
    d.close()


def _seed(db: Db) -> None:
    """A van with live readings and a position, which must never be reported."""
    c = db.conn
    for i in range(20):
        c.execute("INSERT INTO battery_samples (pack_id, ts, soc, voltage, current, power, source) "
                  "VALUES (1, ?, 87.5, 13.31, -4.2, -55.9, 'mqtt')", (NOW - 600 + 30 * i,))
    c.execute("INSERT INTO weather_samples (ts, air_temp_f, lat, lon, position_source) "
              "VALUES (?, 40.1, 12.3456, -98.7654, 'gps')", (NOW - 300,))
    c.execute("INSERT INTO kv (key, value) VALUES ('van.position', '{\"lat\": 12.3456, \"lon\": -98.7654}')")
    for ts in range(NOW - 3600, NOW, 300):
        db.mark_uptime(ts)
    c.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, 'collector_started', 'info', '{}')",
              (NOW - 1800,))


def _report(db, tmp_path, **kw):
    args = dict(device_id=DEVICE, interval_s=900, started_at=NOW - 1800, last_ok_at=None,
                db_path=str(tmp_path / "c.db"), thermal_root=str(tmp_path / "no-thermal"))
    args.update(kw)
    return hr.build(db.conn, NOW, **args)


# -- the report -----------------------------------------------------------------

def test_report_is_box_health_only(db, tmp_path):
    _seed(db)
    r = _report(db, tmp_path)
    assert set(_paths(r)) <= ALLOWED, set(_paths(r)) - ALLOWED
    text = json.dumps(r)
    for leak in ("12.34", "-98.76", "lat", "lon", "13.31", "87.5", "-4.2", "soc", "voltage"):
        assert leak not in text, leak
    assert r["format"] == "cerberus.health" and r["version"] == 1
    assert r["box"]["id"] == DEVICE
    battery = next(s for s in r["sources"] if s["key"] == "battery")
    assert battery == {"key": "battery", "status": "fresh", "age_s": 30}
    assert 0 <= r["coverage_today_pct"] <= 100


def test_since_last(db, tmp_path):
    _seed(db)
    r = _report(db, tmp_path, last_ok_at=NOW - 3000)
    s = r["since_last"]
    assert s["silent_s"] == 3000 and s["last_ok_at"] == NOW - 3000
    assert s["collector_restarts"] == 1
    assert _report(db, tmp_path, last_ok_at=NOW - 60)["since_last"]["collector_restarts"] == 0


def test_collector_down_time(db):
    for ts in (1000, 1300, 1600, 3400, 3700):            # 1600 -> 3400: down ~1500 s
        db.mark_uptime(ts)
    assert hr.collector_down_s(db.conn, 1000, 3700) == 3400 - 1600 - hr.UPTIME_PERIOD
    assert hr.collector_down_s(db.conn, 1000, 1600) == 0
    # both silences count: 1600 -> 3400, and 3700 -> now
    assert hr.collector_down_s(db.conn, 1000, 9000) == (3400 - 1600 - 300) + (9000 - 3700 - 300)


def test_cpu_temp_prefers_the_package(tmp_path):
    for i, (kind, milli) in enumerate((("acpitz", "27800"), ("x86_pkg_temp", "48000"), ("iwlwifi_1", "99000"))):
        z = tmp_path / f"thermal_zone{i}"
        z.mkdir()
        (z / "type").write_text(kind + "\n")
        (z / "temp").write_text(milli + "\n")
    assert hr.cpu_temp_c(str(tmp_path)) == 48.0
    assert hr.cpu_temp_c(str(tmp_path / "none")) is None


# -- sending --------------------------------------------------------------------

class _Sink:
    def __init__(self, status=200):
        self.status, self.got = status, []
        sink = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                sink.got.append((self.headers.get("Authorization"), json.loads(body)))
                self.send_response(sink.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/cerberus/health"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def sink():
    s = _Sink()
    yield s
    s.close()


def _reporter(db, tmp_path, url, enabled=True):
    cfg = HealthReportConfig(enabled=enabled, target_url=url, auth_token="box-secret", interval_seconds=900)
    return hr.Reporter(cfg, db, device_id=DEVICE, db_path=str(tmp_path / "c.db"), started_at=NOW - 60)


def test_off_by_default_sends_nothing(db, tmp_path, sink):
    assert cfg_mod.HealthReportConfig().enabled is False
    r = _reporter(db, tmp_path, sink.url, enabled=False)
    r.start()
    assert r._thread is None
    r.stop()
    time.sleep(0.1)
    assert sink.got == [] and db.get_kv(hr.KV_LAST) is None


def test_send_once_delivers_and_remembers(db, tmp_path, sink):
    _seed(db)
    r = _reporter(db, tmp_path, sink.url)
    assert r.send_once(now=NOW) is True
    auth, body = sink.got[0]
    assert auth == "Bearer box-secret" and body["box"]["id"] == DEVICE
    last = json.loads(db.get_kv(hr.KV_LAST))
    assert last["ok"] is True and last["payload"] == body and last["result"] == "HTTP 200"
    assert db.get_kv(hr.KV_LAST_OK) == str(NOW)
    r.send_once(now=NOW + 900)
    assert sink.got[1][1]["since_last"]["silent_s"] == 900


def test_failure_is_recorded_and_since_last_keeps_counting(db, tmp_path):
    bad = _Sink(status=503)
    try:
        r = _reporter(db, tmp_path, bad.url)
        db.set_kv(hr.KV_LAST_OK, str(NOW - 1000))
        assert r.send_once(now=NOW) is False
        last = json.loads(db.get_kv(hr.KV_LAST))
        assert last["ok"] is False and last["result"] == "HTTP 503"
        assert db.get_kv(hr.KV_LAST_OK) == str(NOW - 1000)
    finally:
        bad.close()
    r = _reporter(db, tmp_path, "http://127.0.0.1:9/nothing-listens")
    assert r.send_once(now=NOW + 10) is False
    assert "not reachable" in json.loads(db.get_kv(hr.KV_LAST))["result"]


# -- config ---------------------------------------------------------------------

def _load(tmp_path, section, env=None, monkeypatch=None):
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    raw["health_report"] = section
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return cfg_mod.load(str(p)).health_report


def test_config_defaults_and_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("CERBERUS_HEALTH_TOKEN", raising=False)
    assert _load(tmp_path, {}) == HealthReportConfig()
    ok = _load(tmp_path, {"enabled": True, "target_url": "https://cp.example/api/v1/cerberus/health",
                          "auth_token": "from-file"})
    assert ok.enabled and ok.auth_token == "from-file" and ok.interval_seconds == 900
    assert _load(tmp_path, {"enabled": True, "target_url": "http://localhost:8770/x"}).enabled
    for bad in ({"enabled": True}, {"enabled": True, "target_url": "http://cp.example/x"},
                {"interval_seconds": 10}):
        with pytest.raises(cfg_mod.ConfigError):
            _load(tmp_path, bad)
    monkeypatch.setenv("CERBERUS_HEALTH_TOKEN", "from-env")
    assert _load(tmp_path, {"auth_token": "from-file"}).auth_token == "from-env"


def test_settings_switch_exists():
    from cerberus import settings
    s = next(s for s in settings.SETTINGS if s.key == "health_report.enabled")
    assert s.kind == "bool" and "Never readings or position" in s.help


# -- the UI ---------------------------------------------------------------------

def test_ui_shows_the_last_report_but_never_the_token(tmp_path, sink):
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    raw["health_report"] = {"enabled": True, "target_url": sink.url, "auth_token": "box-secret"}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw))
    db = Db(str(tmp_path / "s.db"))
    _seed(db)
    hr.Reporter(cfg_mod.load(str(cfg)).health_report, db, device_id=DEVICE,
                db_path=str(tmp_path / "s.db"), started_at=NOW - 60).send_once(now=NOW)
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        c.request("GET", "/api/v1/health-report")
        resp = c.getresponse()
        text = resp.read().decode()
        assert resp.status == 200
        v = json.loads(text)
        assert v["enabled"] is True and v["target_host"] == f"127.0.0.1:{sink.httpd.server_address[1]}"
        assert v["last"]["ok"] is True and v["last"]["payload"]["box"]["id"] == DEVICE
        assert "box-secret" not in text
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()
