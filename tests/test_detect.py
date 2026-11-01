"""Step 5.4b: setup detection — settings proposed from what the Cerbo reports."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus import detect, settings
from cerberus.db import Db
from cerberus.location import KV_POSITION, VanLocation
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_793_480_000


def _real_batteries():
    """One system/0/Batteries message from this van: three Lithionics packs
    and the MultiPlus."""
    with open(FIXTURES / "near_full_postdrive.jsonl") as f:
        return json.loads(f.readline())["value"]


THIS_VAN_HEALTH = {1: {"capacity_ah": 310.0}, 2: {"capacity_ah": 310.0}, 3: {"capacity_ah": 310.0}}

SHUNT = {"id": "com.victronenergy.battery.ttyS5", "instance": 279, "name": "SmartShunt 500A",
         "soc": 71.4, "voltage": 26.4, "active_battery_service": True}
SIDE_BMS = {"id": "com.victronenergy.battery.socketcan_can0_vi0_uc4242_di0", "instance": 512,
            "name": "Some BMS", "soc": 90.0, "voltage": 26.5, "active_battery_service": False}


def _loc(tz="America/Indianapolis", gps=False):
    loc = VanLocation()
    loc.gx_timezone = tz
    loc.gps_seen = gps
    return loc


def _inv(value, health=None, loc=None):
    return detect.inventory(NOW, value, health or {}, loc or _loc())


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, p)
    return p


# --- proposals ----------------------------------------------------------------------

def test_this_van_three_lithionics_packs():
    props, notes = detect.proposals(_inv(_real_batteries(), THIS_VAN_HEALTH))
    assert props["bank.source"]["value"] == "packs"
    assert props["packs"]["value"] == {"uc1000001": 1, "uc1000002": 2, "uc1000003": 3}
    assert "Lithionics" in props["bank.source"]["reason"]
    assert props["bank.capacity_ah"]["value"] == 930 and "3 x 310 Ah" in props["bank.capacity_ah"]["reason"]
    assert props["system.nominal_voltage"]["value"] == 12
    assert props["system.timezone"]["value"] is None
    assert set(props["packs"]["together"]) == set(detect.BANK_KEYS)
    assert notes == ["The Cerbo has no GPS, so set the home ZIP code (or latitude and longitude) by hand; "
                     "the weather forecast uses it."]


def test_capacity_only_when_every_pack_reports_it():
    props, _ = detect.proposals(_inv(_real_batteries(), {1: {"capacity_ah": 310.0}}))
    assert "bank.capacity_ah" not in props


def test_one_battery_is_a_monitor():
    props, _ = detect.proposals(_inv([SHUNT]))
    assert props["bank.source"]["value"] == "monitor"
    assert props["bank.monitor_instance"]["value"] is None and "packs" not in props
    assert props["system.nominal_voltage"]["value"] == 24


def test_a_shunt_beside_another_bms_is_picked_by_instance():
    props, _ = detect.proposals(_inv([SIDE_BMS, SHUNT], {279: {"capacity_ah": 400.0}, 512: {"capacity_ah": 100.0}}))
    assert (props["bank.source"]["value"], props["bank.monitor_instance"]["value"]) == ("monitor", 279)
    assert props["bank.capacity_ah"]["value"] == 400         # the monitor's, not the sum


def test_ambiguous_batteries_are_a_note_not_a_guess():
    other = {**SIDE_BMS, "id": "com.victronenergy.battery.ttyS7", "instance": 7}
    props, notes = detect.proposals(_inv([SIDE_BMS, other]))
    assert "bank.source" not in props and "isn't clear" in notes[0]


def test_nothing_heard_yet():
    props, notes = detect.proposals(None)
    assert props == {} and "hasn't heard" in notes[0]


@pytest.mark.parametrize("volts, nominal", [(13.2, 12), (26.4, 24), (53.0, 48), (19.0, None)])
def test_system_voltage_from_history(volts, nominal):
    props, notes = detect.proposals(_inv(_real_batteries()), voltage=volts)
    if nominal:
        assert props["system.nominal_voltage"]["value"] == nominal
        assert "last 7 days" in props["system.nominal_voltage"]["reason"]
    else:
        assert "system.nominal_voltage" not in props and any("doesn't fit" in n for n in notes)


def test_gps_position():
    props, notes = detect.proposals(_inv(_real_batteries(), loc=_loc(gps=True)),
                                    position={"lat": 39.76841, "lon": -86.15804, "ts": NOW})
    assert (props["weather.latitude"]["value"], props["weather.longitude"]["value"]) == (39.7684, -86.158)
    assert not any("GPS" in n for n in notes)


def test_signature_ignores_voltage_and_time():
    a = _inv(_real_batteries())
    b = detect.inventory(NOW + 60, [{**x, "voltage": 13.9} for x in _real_batteries()], {}, _loc())
    assert detect.signature(a) == detect.signature(b)
    c = _inv(_real_batteries(), THIS_VAN_HEALTH)
    assert detect.signature(a) != detect.signature(c)


# --- applied through settings ---------------------------------------------------------

def _by_key(rows):
    return {r["key"]: r for r in rows}


def test_this_vans_config_matches_what_the_cerbo_reports(cfg):
    props, _ = detect.proposals(_inv(_real_batteries(), THIS_VAN_HEALTH))
    rows = _by_key(settings.current(cfg, props))
    differ = [k for k, r in rows.items() if r["detected"] and not r["detected"]["matches"]]
    assert differ == []


def test_a_detected_pack_map_replaces_the_old_one(cfg):
    settings.apply(cfg, {"packs": {"uc1": 1, "uc2": 2}})
    loaded = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x")
    assert dict(loaded.packs) == {"uc1": 1, "uc2": 2} and loaded.bank.pack_count == 2


def test_switching_to_a_monitor(cfg):
    settings.apply(cfg, {"bank.source": "monitor", "bank.monitor_instance": 279})
    b = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x").bank
    assert (b.source, b.monitor_instance, b.pack_count) == ("monitor", 279, 1)


def test_follow_the_cerbo_overrides_a_configured_zone(cfg):
    cfg.write_text(cfg.read_text().replace("  chemistry: lifepo4", "  chemistry: lifepo4\n  timezone: America/Denver"))
    rows = _by_key(settings.apply(cfg, {"system.timezone": None}))
    assert (rows["system.timezone"]["value"], rows["system.timezone"]["source"]) == (None, "setup")
    rows = _by_key(settings.apply(cfg, {"system.timezone": ""}))       # "" falls back
    assert rows["system.timezone"]["value"] == "America/Denver"


@pytest.mark.parametrize("packs", [{"uc1": 1, "uc2": 1}, {"uc1": 0}, {"uc1": "x"}])
def test_bad_pack_maps_are_refused(cfg, packs):
    with pytest.raises(cfg_mod.ConfigError, match="packs"):
        settings.apply(cfg, {"packs": packs})


# --- collector and server ---------------------------------------------------------------

def test_collector_writes_the_inventory_on_change_only(tmp_path, monkeypatch):
    from cerberus.__main__ import Cerberus
    c = Cerberus.__new__(Cerberus)
    c.db = Db(str(tmp_path / "c.db"))
    c.pack_health, c.location = {}, _loc()
    writes = []
    real = c.db.set_kv
    monkeypatch.setattr(c.db, "set_kv", lambda k, v: (writes.append(k), real(k, v)))
    c._inventory_tick(_real_batteries())
    c._inventory_tick(_real_batteries())                       # unchanged: no write
    c.pack_health = THIS_VAN_HEALTH
    c._inventory_tick(_real_batteries())                       # capacity arrived
    assert writes == [detect.KV_INVENTORY] * 2
    inv = detect.load_inventory(c.db.conn)
    assert [b["capacity_ah"] for b in inv["batteries"]] == [310.0] * 3


def test_location_notices_a_gps_service():
    loc = VanLocation()
    loc.update("N/x/gps/0/Fix", 0)
    assert loc.gps_seen and not loc.fix


@pytest.fixture
def server(cfg, tmp_path):
    db = Db(str(tmp_path / "s.db"))
    db.set_kv(detect.KV_INVENTORY, json.dumps(_inv(_real_batteries(), THIS_VAN_HEALTH)))
    db.set_kv(KV_POSITION, json.dumps({"lat": 39.77, "lon": -86.16, "ts": NOW}))
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_detect_endpoint(server):
    d = _get(server + "/api/v1/setup/detect")
    assert d["inventory"]["gx_timezone"] == "America/Indianapolis"
    assert d["proposals"]["weather.latitude"]["value"] == 39.77


def test_settings_carry_proposals(server):
    d = _get(server + "/api/v1/settings")
    rows = _by_key(d["settings"])
    assert rows["packs"]["detected"]["matches"] is True
    assert rows["weather.latitude"]["detected"]["matches"] is False      # home vs last fix
    assert d["detect"]["ts"] == NOW
