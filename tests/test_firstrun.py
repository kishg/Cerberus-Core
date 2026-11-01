"""Step 5.4c: first-run setup — finding the Cerbo, checking its password,
writing the first config, and the collector waiting for it. Offline: the
network is faked and the probe is fed recorded messages."""

from __future__ import annotations

import ipaddress
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import envfile, firstrun
from cerberus.__main__ import wait_for_config
from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

FIXTURES = Path(__file__).parent / "fixtures"
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
PORTAL = "0123456789ab"


@pytest.fixture(autouse=True)
def no_env_password(monkeypatch):
    monkeypatch.delenv(envfile.MQTT_PASSWORD, raising=False)
    monkeypatch.delenv("CERBERUS_SQLITE_PATH", raising=False)


def _batteries():
    with open(FIXTURES / "near_full_postdrive.jsonl") as f:
        return json.loads(f.readline())["value"]


def _msg(v):
    return json.dumps({"value": v}).encode()


# --- .env -------------------------------------------------------------------------

def test_env_file_keeps_other_lines_and_is_private(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# keys\nGOVEE_API_KEY=abc\nexport CERBERUS_MQTT_PASSWORD=\"old\"\nVRM_TOKEN='t'\n")
    envfile.set_value(p, envfile.MQTT_PASSWORD, "n3w pass")
    assert p.read_text() == "# keys\nGOVEE_API_KEY=abc\nCERBERUS_MQTT_PASSWORD='n3w pass'\nVRM_TOKEN='t'\n"
    assert (p.stat().st_mode & 0o777) == 0o600
    assert envfile.read(p) == {"GOVEE_API_KEY": "abc", envfile.MQTT_PASSWORD: "n3w pass", "VRM_TOKEN": "t"}


@pytest.mark.parametrize("bad", ["", "it's", "two\nlines"])
def test_env_file_refuses_values_it_cannot_quote(tmp_path, bad):
    with pytest.raises(envfile.EnvFileError):
        envfile.set_value(tmp_path / ".env", "K", bad)


# --- config on a box not set up yet ------------------------------------------------------

def test_no_config_is_not_set_up(tmp_path):
    cfg = tmp_path / "config.yaml"
    assert not cfg_mod.is_set_up(cfg)
    with pytest.raises(cfg_mod.ConfigError, match="not set up yet"):
        cfg_mod.load(cfg)
    assert cfg_mod.load_ui(cfg).pack_count == 1          # the UI still starts


def test_password_is_read_from_env_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    with pytest.raises(cfg_mod.ConfigError, match="CERBERUS_MQTT_PASSWORD"):
        cfg_mod.load(cfg)
    envfile.set_value(envfile.path_for(cfg), envfile.MQTT_PASSWORD, "pw")
    assert cfg_mod.load(cfg).mqtt.password == "pw"


# --- discovery ---------------------------------------------------------------------------

def test_discover_puts_the_gx_first():
    open_ = {"10.0.0.2", "10.0.0.5"}                     # .2 is Home Assistant's broker
    found = firstrun.discover(
        networks=[ipaddress.ip_network("10.0.0.0/29")],
        port_check=lambda h: h in open_,
        gx_check=lambda h: h == "10.0.0.5",
        resolver=lambda name: "10.0.0.5")
    assert found == [{"host": "venus.local", "address": "10.0.0.5", "gx": True},
                     {"host": "10.0.0.2", "address": "10.0.0.2", "gx": False}]


# --- probe -------------------------------------------------------------------------------

def test_probe_builds_proposals_from_what_the_gx_says():
    p = firstrun.Probe()
    p.message(f"N/{PORTAL}/system/0/Serial", _msg(PORTAL))
    p.message(f"N/{PORTAL}/settings/0/Settings/System/TimeZone", _msg("America/Indianapolis"))
    p.message(f"N/{PORTAL}/system/0/Batteries", _msg(_batteries()), now=100.0)
    for inst in (1, 2, 3):
        p.message(f"N/{PORTAL}/battery/{inst}/Capacity", _msg(310.0))
    assert not p.done(now=101.0) and p.done(now=100.0 + firstrun.SETTLE_S)
    r = p.result()
    assert r["portal_id"] == PORTAL
    props = r["proposals"]
    assert props["packs"]["value"] == {"uc1000001": 1, "uc1000002": 2, "uc1000003": 3}
    assert props["bank.capacity_ah"]["value"] == 930
    assert props["system.nominal_voltage"]["label"] == "System voltage"
    assert props["system.timezone"]["display"] == "Follow the Cerbo (America/Indianapolis)"


def test_probe_ignores_junk():
    p = firstrun.Probe()
    p.message("N/x/system/0/Batteries", b"not json")
    p.message("N/x/system/0/Serial", b"")
    assert p.portal_id is None and not p.done(now=1e9)


# --- complete --------------------------------------------------------------------------------

CHANGES = {"bank.source": "packs", "bank.monitor_instance": None,
           "packs": {"uc1000001": 1, "uc1000002": 2, "uc1000003": 3},
           "bank.capacity_ah": 930, "system.timezone": None}


def test_complete_writes_a_config_that_loads(tmp_path):
    cfg = tmp_path / "config.yaml"
    firstrun.complete(cfg, "192.168.1.60", "192.168.1.60", "secret", PORTAL, CHANGES,
                      str(tmp_path / "c.db"))
    assert "secret" not in cfg.read_text() and "secret" not in cfg_mod.setup_path(cfg).read_text()
    assert (envfile.path_for(cfg).stat().st_mode & 0o777) == 0o600
    loaded = cfg_mod.load(cfg)
    assert loaded.mqtt.hosts == ("192.168.1.60", "venus.local")
    assert (loaded.mqtt.portal_id, loaded.mqtt.password) == (PORTAL, "secret")
    assert loaded.bank.pack_count == 3 and loaded.bank.capacity_ah == 930
    assert cfg_mod.is_set_up(cfg)
    with pytest.raises(firstrun.SetupError, match="already set up"):
        firstrun.complete(cfg, "x", "x", "secret", PORTAL, CHANGES, str(tmp_path / "c.db"))


def test_complete_writes_nothing_if_the_result_would_not_load(tmp_path):
    cfg = tmp_path / "config.yaml"
    with pytest.raises(cfg_mod.ConfigError, match="packs map is empty"):
        firstrun.complete(cfg, "h", "h", "secret", PORTAL, {}, str(tmp_path / "c.db"))
    with pytest.raises(firstrun.SetupError, match="Password"):
        firstrun.complete(cfg, "h", "h", "it's", PORTAL, CHANGES, str(tmp_path / "c.db"))
    assert list(tmp_path.iterdir()) == []


# --- the collector waits ------------------------------------------------------------------------

def test_collector_waits_then_starts_once_setup_is_written(tmp_path):
    cfg = tmp_path / "config.yaml"
    stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "ok", wait_for_config(cfg, cfg_mod.ConfigError(cfg_mod.NOT_SET_UP), poll_s=0.05, stop=stop)))
    t.start()
    firstrun.complete(cfg, "h", "h", "secret", PORTAL, CHANGES, str(tmp_path / "c.db"))
    t.join(5)
    stop.set()
    assert result == {"ok": True}


def test_waiting_stops_on_shutdown(tmp_path):
    stop = threading.Event()
    stop.set()
    assert wait_for_config(tmp_path / "config.yaml", FileNotFoundError(), poll_s=0.01, stop=stop) is False


# --- server ------------------------------------------------------------------------------------

def _req(url, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                               headers={"Content-Type": "application/json",
                                        **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _serve(cfg, db_path):
    httpd = serve(App.from_config(str(cfg), str(db_path)), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A new box: no config.yaml, a PIN, and a Cerbo faked at the probe."""
    cfg = tmp_path / "config.yaml"
    seen = []

    def fake_probe(address, password, timeout=0):
        seen.append((address, password))
        if password != "secret":
            raise firstrun.SetupError("Wrong password. Use the Cerbo's Remote Console password.")
        p = firstrun.Probe()
        p.message(f"N/{PORTAL}/system/0/Serial", _msg(PORTAL))
        p.message(f"N/{PORTAL}/system/0/Batteries", _msg(_batteries()))
        return p.result()

    monkeypatch.setattr(firstrun, "probe", fake_probe)
    monkeypatch.setattr(firstrun, "discover", lambda: [{"host": "venus.local", "address": "10.0.0.5", "gx": True}])
    httpd, url = _serve(cfg, tmp_path / "c.db")
    _, r = _req(url + "/api/v1/auth/pin", {"pin": "2468"})
    yield cfg, url, r["token"], seen
    httpd.shutdown()
    httpd.server_close()


def test_a_fresh_box_reports_it_is_not_set_up(fresh):
    cfg, url, token, _ = fresh
    assert _req(url + "/api/v1/about")[1]["set_up"] is False
    assert _req(url + "/api/v1/settings")[1]["set_up"] is False
    assert _req(url + "/api/v1/setup/status")[1] == {"set_up": False, "connection": None, "pin_set": True}


def test_setup_needs_a_session(fresh):
    cfg, url, token, seen = fresh
    assert _req(url + "/api/v1/setup/discover", {})[0] == 401
    assert _req(url + "/api/v1/setup/probe", {"address": "10.0.0.5", "password": "secret"})[0] == 401
    assert seen == []


def test_wizard_flow(fresh):
    cfg, url, token, seen = fresh
    code, d = _req(url + "/api/v1/setup/discover", {}, token)
    assert code == 200 and d["candidates"][0]["gx"]
    code, err = _req(url + "/api/v1/setup/probe", {"address": "10.0.0.5", "password": "nope"}, token)
    assert code == 400 and "Wrong password" in err["error"]
    code, r = _req(url + "/api/v1/setup/probe", {"address": "10.0.0.5", "password": "secret"}, token)
    assert code == 200 and r["portal_id"] == PORTAL
    changes = {k: r["proposals"][k]["value"] for k in ("bank.source", "bank.monitor_instance", "packs")}
    code, r = _req(url + "/api/v1/setup/complete",
                   {"address": "10.0.0.5", "host": "venus.local", "password": "secret", "changes": changes}, token)
    assert code == 200 and r["portal_id"] == PORTAL
    assert yaml.safe_load(cfg.read_text())["mqtt"]["hosts"] == ["10.0.0.5", "venus.local"]
    status = _req(url + "/api/v1/setup/status")[1]
    assert status["set_up"] and status["connection"]["password_set"]
    assert "secret" not in json.dumps(status)


def test_changing_the_password_is_checked_on_the_cerbo_first(fresh):
    cfg, url, token, seen = fresh
    firstrun.complete(cfg, "10.0.0.5", "10.0.0.5", "secret", PORTAL, CHANGES, str(cfg.parent / "c.db"))
    code, err = _req(url + "/api/v1/setup/password", {"password": "nope"}, token)
    assert code == 400 and envfile.mqtt_password(cfg) == "secret"
    code, _ = _req(url + "/api/v1/setup/password", {"password": "secret"}, token)
    assert code == 200
