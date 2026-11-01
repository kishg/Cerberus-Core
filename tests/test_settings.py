"""Step 5.4a: owner settings (setup.yaml), the settings PIN, the v1 API's
write path, and the collector restarting into new settings."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import settings
from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


@pytest.fixture
def cfg(tmp_path):
    """A copy of this van's real, heavily commented config.yaml."""
    p = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, p)
    return p


def _by_key(rows):
    return {r["key"]: r for r in rows}


# --- settings ---------------------------------------------------------------------

def test_values_and_where_they_came_from(cfg):
    rows = _by_key(settings.current(cfg))
    assert rows["solar.array_w"]["value"] == 300 and rows["solar.array_w"]["source"] == "config"
    assert rows["system.timezone"]["value"] is None and rows["system.timezone"]["source"] == "default"


def test_apply_writes_setup_yaml_and_never_touches_config(cfg):
    before = cfg.read_bytes()
    rows = _by_key(settings.apply(cfg, {"solar.array_w": "400", "system.timezone": "America/Denver"}))
    assert cfg.read_bytes() == before                       # comments and all
    assert yaml.safe_load(cfg_mod.setup_path(cfg).read_text()) == {
        "solar": {"array_w": 400}, "system": {"timezone": "America/Denver"}}
    assert (rows["solar.array_w"]["value"], rows["solar.array_w"]["source"]) == (400, "setup")
    # And the collector's loader sees it, derived thresholds included.
    loaded = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x")
    assert loaded.solar.array_w == 400 and loaded.timezone == "America/Denver"


@pytest.mark.parametrize("changes, match", [
    ({"system.nominal_voltage": "36"}, "must be one of"),
    ({"solar.array_w": "lots"}, "not a number"),
    ({"solar.array_w": "-5"}, "solar"),
    ({"system.timezone": "Mars/Base"}, "timezone"),
    ({"mqtt.host": "evil"}, "not an editable setting"),
])
def test_invalid_changes_write_nothing(cfg, changes, match):
    with pytest.raises(cfg_mod.ConfigError, match=match):
        settings.apply(cfg, changes)
    assert not cfg_mod.setup_path(cfg).exists()


def test_clearing_falls_back_to_config(cfg):
    settings.apply(cfg, {"bank.capacity_ah": "620"})
    rows = _by_key(settings.apply(cfg, {"bank.capacity_ah": ""}))
    assert (rows["bank.capacity_ah"]["value"], rows["bank.capacity_ah"]["source"]) == (930, "config")


# --- PIN and sessions -----------------------------------------------------------------

def test_pin_is_stored_hashed_and_private(cfg):
    auth.set_pin(cfg, "2468")
    p = auth.pin_path(cfg)
    assert "2468" not in p.read_text() and (p.stat().st_mode & 0o777) == 0o600
    assert auth.check_pin(cfg, "2468") and not auth.check_pin(cfg, "1357")


@pytest.mark.parametrize("pin", ["123", "abcd", "1234567890123", ""])
def test_pin_format(cfg, pin):
    with pytest.raises(auth.AuthError):
        auth.set_pin(cfg, pin)


def test_lockout_after_repeated_wrong_pins_and_session_expiry(cfg):
    auth.set_pin(cfg, "2468")
    now = [1000.0]
    s = auth.Sessions(cfg, clock=lambda: now[0])
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError, match="Wrong PIN"):
            s.login("0000", "10.0.0.5")
    with pytest.raises(auth.AuthError, match="Too many"):
        s.login("2468", "10.0.0.5")                     # even the right one
    tok = s.login("2468", "10.0.0.6")                   # per address
    assert s.valid(tok)
    now[0] += auth.LOCKOUT_S
    assert s.login("2468", "10.0.0.5")
    now[0] += auth.SESSION_S + 1
    assert not s.valid(tok)


def test_loopback_detection():
    assert auth.is_loopback("127.0.0.1") and auth.is_loopback("::1")
    assert not auth.is_loopback("192.168.1.86") and not auth.is_loopback("100.125.190.101")


# --- server write path --------------------------------------------------------------------

@pytest.fixture
def server(cfg, tmp_path):
    db = Db(str(tmp_path / "s.db"))
    db.set_kv("device.id", "box-123")
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


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


def test_about_reports_device_and_pin_state(server, cfg):
    code, about = _req(server + "/api/v1/about")
    assert code == 200 and about["device_id"] == "box-123" and about["pin_set"] is False
    assert _req(server + "/api/about")[0] == 200            # pre-v1 alias


def test_changing_settings_needs_a_session(server, cfg):
    code, err = _req(server + "/api/v1/settings", {"changes": {"solar.array_w": 400}})
    assert code == 401 and not cfg_mod.setup_path(cfg).exists()


def test_first_pin_then_save(server, cfg):
    code, r = _req(server + "/api/v1/auth/pin", {"pin": "2468"})      # tests run on loopback
    assert code == 200 and r["token"]
    assert _req(server + "/api/v1/auth/pin", {"pin": "1111"})[0] == 409
    code, r = _req(server + "/api/v1/settings", {"changes": {"solar.array_w": 400}}, r["token"])
    assert code == 200 and r["restarting"] is True
    assert _by_key(r["settings"])["solar.array_w"]["source"] == "setup"


def test_bad_settings_are_a_400_with_the_reason(server, cfg):
    _, r = _req(server + "/api/v1/auth/pin", {"pin": "2468"})
    code, err = _req(server + "/api/v1/settings", {"changes": {"system.nominal_voltage": 36}}, r["token"])
    assert code == 400 and "must be one of" in err["error"]


def test_wrong_pin_is_refused(server, cfg):
    auth.set_pin(cfg, "2468")
    code, err = _req(server + "/api/v1/auth/login", {"pin": "0000"})
    assert code == 403 and err["error"] == "Wrong PIN"


# --- collector restarts into new settings -------------------------------------------------

def _collector(cfg, tmp_path):
    from cerberus.__main__ import Cerberus
    c = Cerberus.__new__(Cerberus)
    c.db = Db(str(tmp_path / "c.db"))
    c.watch_config(cfg)
    return c


def test_collector_restarts_into_a_valid_change(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    c = _collector(cfg, tmp_path)
    assert not c._config_changed()
    settings.apply(cfg, {"solar.array_w": 400})
    assert c._config_changed() and c.restart_requested


def test_collector_ignores_a_change_that_does_not_load(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    c = _collector(cfg, tmp_path)
    cfg_mod.setup_path(cfg).write_text("system: {nominal_voltage: 36}\n")    # a bad hand edit
    assert not c._config_changed() and not c.restart_requested


def test_device_id_is_created_once(cfg, tmp_path):
    c = _collector(cfg, tmp_path)
    first = c.ensure_device_id()
    assert len(first) == 36 and c.ensure_device_id() == first


# --- persistent sessions -------------------------------------------------------------

def test_sessions_survive_a_restart_and_store_no_tokens(cfg):
    auth.set_pin(cfg, "2468")
    tok = auth.Sessions(cfg).login("2468", "10.0.0.5")
    assert auth.Sessions(cfg).valid(tok)                      # a new process: still signed in
    f = auth.sessions_path(cfg)
    assert tok not in f.read_text() and (f.stat().st_mode & 0o777) == 0o600


def test_sessions_slide_and_expire(cfg):
    auth.set_pin(cfg, "2468")
    now = [1000.0]
    s = auth.Sessions(cfg, clock=lambda: now[0])
    tok = s.login("2468", "10.0.0.5")
    now[0] += auth.SESSION_S - 60
    assert s.valid(tok)                                       # used: renewed for another 30 days
    now[0] += auth.SESSION_S - 60
    assert s.valid(tok)
    now[0] += auth.SESSION_S + 1                              # unused for 30 days
    assert not s.valid(tok)


def test_sign_out_and_new_pin_end_sessions(cfg):
    auth.set_pin(cfg, "2468")
    running = auth.Sessions(cfg)
    a, b = running.login("2468", "x"), running.login("2468", "y")
    running.logout(a)
    assert not auth.Sessions(cfg).valid(a) and running.valid(b)
    auth.set_pin(cfg, "1357")                                 # e.g. `--set-pin` on the box
    assert not running.valid(b)                               # the running UI notices


def test_files_written_beside_config_are_never_committed():
    """10-31: the phone position file was committed with `git add -A`."""
    import subprocess
    from cerberus import envfile, notes, phonefix
    root = REPO_CONFIG.parent
    names = [auth.PIN_FILE, auth.SESSIONS_FILE, phonefix.PHONE_FILE, cfg_mod.SETUP_FILE, envfile.ENV_FILE,
             notes.NOTES_FILE]
    for n in names:
        assert subprocess.run(["git", "check-ignore", "-q", n], cwd=root).returncode == 0, n
