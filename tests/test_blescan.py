"""Finding Bluetooth devices from Settings (cerberus.blescan)."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cerberus import blescan
from cerberus import config as cfg_mod
from cerberus import settings
from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
# Real frames, as the sidecar's parsers document them.
CELLS = "1336,334,334,334,334,80,73,0,99,000000"
STATUS = "&,3,306,589413,0144,2300,FF03,0000"
ACR = "*,0,0,1261,1325,0,0,0,1,6002"


def test_roles_come_from_what_a_device_streams():
    assert blescan.classify_lines([CELLS, STATUS, CELLS]) == ("pack", 3, "13.36 V, 99%")
    assert blescan.classify_lines([ACR, ACR]) == ("acr", None, "house 13.25 V")
    assert blescan.classify_lines(["junk", ""]) == ("unknown", None, "")


@pytest.mark.parametrize("name, uuids, yes", [
    ("Li3-000000001", [], True), ("Lithionics ACR", [], True), ("", [blescan.UART_SERVICE], True),
    ("Owner's iPhone", [], False), ("[TV] Family room TV", [], False)])
def test_lithionics_candidates(name, uuids, yes):
    assert blescan.lithionics_candidate(name, uuids) is yes


def _found(role, addr, name="", n=None, rssi=-60, detail=""):
    return {"address": addr, "name": name, "rssi": rssi, "role": role, "pack_number": n, "detail": detail}


def test_packs_numbered_by_their_own_numbers():
    found = [_found("pack", "C", "Li3-000000003", 3), _found("pack", "A", "Li3-000000001", 1),
             _found("pack", "B", "Li3-000000002", 2), _found("acr", "R"),
             _found("sensor", "S1", rssi=-80, detail="Govee H5075: 70 °F"),
             _found("sensor", "S2", rssi=-50, detail="Govee H5179: 68 °F")]
    p = blescan.proposals(found)
    assert p["ble.pack_addresses"]["value"] == {1: "A", 2: "B", 3: "C"}
    assert "own pack numbers" in p["ble.pack_addresses"]["reason"]
    assert p["ble.acr_address"]["value"] == "R"
    assert p["ble.cabin_sensors"]["value"] == {"S2": "cabin"}            # strongest only
    assert p["ble.enabled"]["value"] is True


def test_packs_without_distinct_numbers_go_by_serial():
    found = [_found("pack", "C", "Li3-054", 1), _found("pack", "A", "Li3-044", 1)]
    p = blescan.proposals(found)
    assert p["ble.pack_addresses"]["value"] == {1: "A", 2: "C"} and "serial" in p["ble.pack_addresses"]["reason"]


def test_nothing_found_proposes_nothing():
    assert blescan.proposals([]) == {}


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, p)
    return p


def test_scanned_devices_replace_the_configured_ones(cfg):
    settings.apply(cfg, {"ble.pack_addresses": {"1": "AA:BB", "2": "CC:DD"},
                         "ble.cabin_sensors": {"11:22": "fridge"}})
    ble = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x").ble
    assert dict(ble.pack_addresses) == {1: "AA:BB", 2: "CC:DD"}           # pack 3 gone, not merged
    assert dict(ble.cabin_sensors) == {"11:22": "fridge"}


def test_a_sensor_needs_a_name(cfg):
    with pytest.raises(cfg_mod.ConfigError, match="needs a name"):
        settings.apply(cfg, {"ble.cabin_sensors": {"11:22": " "}})


def test_scan_endpoint(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(blescan, "scan", lambda: [_found("pack", "A", "Li3-1", 1)])
    httpd = serve(App.from_config(str(cfg), Db(str(tmp_path / "s.db"))._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"

    def post(path, body, tok=None):
        r = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json",
                                            **({"Authorization": f"Bearer {tok}"} if tok else {})})
        try:
            with urllib.request.urlopen(r, timeout=5) as x:
                return x.status, json.loads(x.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert post("/setup/ble/scan", {})[0] == 401
        auth.set_pin(cfg, "2468")
        tok = post("/auth/login", {"pin": "2468"})[1]["token"]
        code, r = post("/setup/ble/scan", {}, tok)
        assert code == 200 and r["proposals"]["ble.pack_addresses"]["value"] == {"1": "A"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_van_with_only_a_temperature_sensor(cfg):
    """Not every van has Lithionics packs; a cabin sensor alone is enough."""
    settings.apply(cfg, {"ble.pack_addresses": {}, "ble.acr_address": "",
                         "ble.cabin_sensors": {"11:22": "cabin"}})
    ble = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x").ble
    assert ble.enabled and dict(ble.cabin_sensors) == {"11:22": "cabin"} and not ble.pack_addresses


def test_sidecar_runs_for_a_sensor_alone(monkeypatch, tmp_path):
    """10-31 container test: BLE on with only a cabin sensor made the sidecar
    log "nothing to collect" and exit, a restart loop under systemd."""
    import asyncio
    from cerberus import ble as ble_mod
    from cerberus.db import Db
    ran = []

    async def fake_cabin(self):
        ran.append("cabin")

    monkeypatch.setattr(ble_mod.BleCollector, "_cabin_loop", fake_cabin)
    cfg = cfg_mod.BleConfig(cabin_sensors={"AA:BB": "cabin"}, cabin_sample_seconds=60, enabled=True,
                            pack_addresses={}, sample_downsample_seconds=30,
                            reconnect_backoff_seconds=5, connect_timeout_seconds=60.0)
    asyncio.run(ble_mod.BleCollector(cfg, Db(str(tmp_path / "b.db"))).run())
    assert ran == ["cabin"]
