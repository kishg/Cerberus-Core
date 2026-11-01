"""Step 5.5b: the owner's phone as the van's position, only while it is with
the van (cerberus.phonefix), and the proxy-aware "is this the box" test."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from cerberus import location, phonefix
from cerberus.db import Db
from cerberus.location import VanLocation
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
NOW = 1_793_480_000
TS_HOST = "vanbox.tail1234.ts.net"


# --- who is asking ------------------------------------------------------------------

@pytest.mark.parametrize("addr, headers, kind", [
    ("127.0.0.1", {"Host": "localhost:8088"}, "box"),
    ("::1", {"Host": "[::1]:8088"}, "box"),
    ("::ffff:127.0.0.1", {"Host": "127.0.0.1:8088"}, "box"),
    ("127.0.0.1", {"Host": TS_HOST}, "tailscale"),                     # tailscale serve
    ("127.0.0.1", {"Host": "localhost:8088", "X-Forwarded-For": "100.1.2.3"}, "tailscale"),
    ("127.0.0.1", {"Host": "localhost", "Tailscale-User-Login": "x@y"}, "tailscale"),
    ("127.0.0.1", {}, "tailscale"),                                     # no Host: be cautious
    ("192.168.1.20", {"Host": "192.168.1.86:8088"}, "lan"),
    ("100.101.1.2", {"Host": "100.125.190.101:8088"}, "tailscale"),
    ("fd7a:115c:a1e0::5", {}, "tailscale"),
    ("not-an-ip", {}, "tailscale"),
])
def test_client_kind(addr, headers, kind):
    assert phonefix.client_kind(addr, headers) == kind


# --- the rules ----------------------------------------------------------------------

FIX = {"lat": 39.7684, "lon": -86.158, "accuracy_m": 30, "ts": NOW}


@pytest.mark.parametrize("kind, fix, moving, ok, says", [
    ("lan", FIX, None, True, "van's network"),
    ("box", FIX, None, True, "van's network"),
    ("tailscale", FIX, None, False, "outside the van's network"),
    ("tailscale", FIX, "the engine is running", True, "moving"),
    ("lan", {**FIX, "ts": NOW - 600}, None, False, "out of date"),
    ("lan", {**FIX, "accuracy_m": 5000}, None, False, "5.0 km"),
])
def test_judge(kind, fix, moving, ok, says):
    got, reason = phonefix.judge(kind, fix, NOW, moving)
    assert got is ok and says in reason


@pytest.mark.parametrize("body", [{}, {"lat": "x", "lon": 1}, {"lat": 91, "lon": 0},
                                  {"lat": 0, "lon": 0}])
def test_bad_fixes(body):
    with pytest.raises(ValueError):
        phonefix.parse_fix(body, NOW)


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "p.db"))


def _snapshot(db, ts, bank_a, solar_a=0.0, ac_charging=False):
    db.set_kv("live.snapshot", json.dumps({"ts": ts, "bank": {"current": bank_a},
                                           "solar": {"charge_a": solar_a},
                                           "ac": {"charging_from_ac": ac_charging}}))


def test_moving_from_engine_current(db):
    _snapshot(db, NOW - 5, 150.0, solar_a=10.0)
    assert "140 A" in phonefix.moving(db.conn, NOW, 20.0)
    _snapshot(db, NOW - 5, 150.0, ac_charging=True)                    # the shore charger
    assert phonefix.moving(db.conn, NOW, 20.0) is None
    _snapshot(db, NOW - 600, 150.0)                                    # stale snapshot
    assert phonefix.moving(db.conn, NOW, 20.0) is None


def test_moving_from_obd(db):
    db.conn.execute("INSERT INTO engine_samples (ts, rpm) VALUES (?, 1800)", (NOW - 60,))
    assert phonefix.moving(db.conn, NOW, 20.0) == "the engine is running"


# --- where the van is ------------------------------------------------------------------

def test_precedence_gps_then_phone_then_last_then_home():
    loc = VanLocation()
    assert loc.position(1.0, 2.0, now=NOW) == (1.0, 2.0, "config")
    loc.phone_fix(39.77, -86.16, NOW)
    assert loc.position(1.0, 2.0, now=NOW + 60) == (39.77, -86.16, "phone")
    assert loc.position(1.0, 2.0, now=NOW + location.PHONE_STALE_SECONDS + 1) == (39.77, -86.16, "phone_last")
    loc.update("N/x/gps/0/Position/Latitude", 40.0, now=NOW)
    loc.update("N/x/gps/0/Position/Longitude", -75.0, now=NOW)
    loc.update("N/x/gps/0/Fix", 1, now=NOW)
    assert loc.position(1.0, 2.0, now=NOW + 60) == (40.0, -75.0, "gps")


def test_a_phone_fix_survives_a_restart(db):
    VanLocation(db.kv_mapping()).phone_fix(39.77, -86.16, NOW)
    assert VanLocation(db.kv_mapping()).position(1.0, 2.0, now=NOW) == (39.77, -86.16, "phone_last")


def test_collector_takes_up_the_file(tmp_path):
    from cerberus.__main__ import Cerberus
    cfg = tmp_path / "config.yaml"
    c = Cerberus.__new__(Cerberus)
    c.config_path, c.location = cfg, VanLocation()
    c._phone_tick()                                                     # no file: nothing
    phonefix.write(cfg, {**FIX, "ts": NOW}, "lan")
    c._phone_tick()
    assert c.location.phone == (39.7684, -86.158, NOW)
    assert (phonefix.path_for(cfg).stat().st_mode & 0o777) == 0o600


# --- server ------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    db = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg, db, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body, host="localhost", token=None):
    """POST with a chosen Host header, as `tailscale serve` would send it."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1" + path, body=json.dumps(body),
              headers={"Host": host, "Content-Type": "application/json",
                       **({"Authorization": f"Bearer {token}"} if token else {})})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_first_pin_through_tailscale_serve_is_refused(server):
    """Security: tailscale serve connects from localhost; that must not count
    as being at the box."""
    cfg, _, port = server
    code, err = _post(port, "/auth/pin", {"pin": "2468"}, host=TS_HOST)
    assert code == 403 and not auth.has_pin(cfg)
    assert _post(port, "/auth/pin", {"pin": "2468"}, host="localhost:8088")[0] == 200


def test_phone_fix_rules_through_the_api(server):
    cfg, db, port = server
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    body = {"lat": 39.7684, "lon": -86.158, "accuracy_m": 25,
            "ts_ms": int(time.time() * 1000)}
    assert _post(port, "/location/phone", body)[0] == 401
    code, r = _post(port, "/location/phone", body, host=TS_HOST, token=tok)
    assert code == 200 and not r["accepted"] and phonefix.read(cfg) is None
    _snapshot(db, int(time.time()), 160.0)
    code, r = _post(port, "/location/phone", body, host=TS_HOST, token=tok)
    assert r["accepted"] and r["via"] == "tailscale" and phonefix.read(cfg)["lat"] == 39.7684
    code, r = _post(port, "/location/phone", body, host="localhost:8088", token=tok)
    assert r["accepted"] and r["via"] == "box"


# --- over Tailscale, but on the van's network ------------------------------------------

@pytest.mark.parametrize("addr, headers, peer", [
    ("127.0.0.1", {"Host": TS_HOST, "X-Forwarded-For": "100.64.0.10"}, "100.64.0.10"),
    ("127.0.0.1", {"Host": TS_HOST, "X-Forwarded-For": "100.64.0.10, 10.0.0.1"}, "100.64.0.10"),
    ("100.64.0.10", {}, "100.64.0.10"),                           # http://<tailscale ip>:8088
    ("127.0.0.1", {"Host": TS_HOST}, None),                           # serve without the header
    ("192.168.1.20", {}, None),
])
def test_tailnet_peer(addr, headers, peer):
    assert phonefix.tailnet_peer(addr, headers) == peer


def test_local_over_tailscale_is_with_the_van():
    ok, reason = phonefix.judge(phonefix.TAILSCALE_LOCAL, FIX, NOW, None)
    assert ok and "directly on the van's network" in reason


def test_phone_on_the_lan_through_tailscale_serve(server, monkeypatch):
    """The 10-31 report: a phone at home used the ts.net address and was
    refused, though Tailscale reached it on the same LAN as the box."""
    cfg, db, port = server
    from cerberus.ui import access
    monkeypatch.setattr(access, "peer_on_lan", lambda ip: "192.168.1.61:41641" if ip == "100.64.0.10" else None)
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    body = {"lat": 40.86, "lon": -74.41, "accuracy_m": 20, "ts_ms": int(time.time() * 1000)}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1/location/phone", body=json.dumps(body),
              headers={"Host": TS_HOST, "X-Forwarded-For": "100.64.0.10",
                       "Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    r = json.loads(c.getresponse().read())
    assert r["accepted"] and r["via"] == "tailscale_local"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1/location/phone", body=json.dumps(body),
              headers={"Host": TS_HOST, "X-Forwarded-For": "100.65.1.1",
                       "Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    assert json.loads(c.getresponse().read())["accepted"] is False
