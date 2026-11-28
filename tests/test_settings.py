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
    assert rows["thresholds.def_low_pct"]["value"] == 20


def test_def_low_pct_is_editable_and_keeps_the_other_thresholds(cfg):
    rows = _by_key(settings.apply(cfg, {"thresholds.def_low_pct": "50"}))
    assert rows["thresholds.def_low_pct"]["value"] == 50
    assert rows["thresholds.def_low_pct"]["source"] == "setup"
    assert cfg_mod.load_ui(cfg).def_low_pct == 50.0


def test_temperature_unit_defaults_to_f_and_is_editable(cfg):
    assert _by_key(settings.current(cfg))["system.temperature_unit"]["value"] == "F"
    rows = _by_key(settings.apply(cfg, {"system.temperature_unit": "C"}))
    assert rows["system.temperature_unit"]["value"] == "C"
    assert cfg_mod.load_ui(cfg).temperature_unit == "C"


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
    ({"thresholds.def_low_pct": "150"}, "def_low_pct"),
    ({"system.temperature_unit": "K"}, "must be one of"),
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


def test_a_relayed_request_cannot_set_the_first_pin(server, cfg):
    # From loopback, as a relay's agent forwards it, with a local-looking Host: still refused.
    r = urllib.request.Request(server + "/api/v1/auth/pin", data=json.dumps({"pin": "2468"}).encode(),
                               method="POST", headers={"Content-Type": "application/json",
                                                       "Host": "127.0.0.1", "X-Cerberus-Via": "relay"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r, timeout=5)
    assert e.value.code == 403
    assert _req(server + "/api/v1/about")[1]["pin_set"] is False


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
    from cerberus import envfile, notes, phonefix, service_log
    root = REPO_CONFIG.parent
    names = [auth.PIN_FILE, auth.SESSIONS_FILE, phonefix.PHONE_FILE, cfg_mod.SETUP_FILE, envfile.ENV_FILE,
             notes.NOTES_FILE, service_log.SERVICE_FILE]
    for n in names:
        assert subprocess.run(["git", "check-ignore", "-q", n], cwd=root).returncode == 0, n


# --- the box's own Wi-Fi network (Core #53) ------------------------------------------------

def test_wifi_view_shows_the_network_and_the_password_only_with_a_session(cfg, tmp_path):
    from cerberus import wifiap
    from cerberus.secretstore import SecretStore
    from cerberus.ui.server import App, serve
    db = Db(str(tmp_path / "w.db"))
    app = App.from_config(str(cfg), db._path)
    app.wifi_status_dir = tmp_path / "wifi"
    wifiap.write_status({"state": "on", "ssid": "Cerberus-AB12", "address": "10.42.0.1", "security": "WPA2/WPA3",
                         "channel": 6, "clients": 1, "since": 100}, app.wifi_status_dir)
    SecretStore.for_config(cfg).set(wifiap.SECRET, "Pw23456789ab")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, w = _req(base + "/api/v1/wifi")
        assert code == 200 and (w["enabled"], w["state"], w["ssid"], w["url"], w["clients"]) == \
               (True, "on", "Cerberus-AB12", "http://10.42.0.1:8088", 1)
        assert "password" not in w                                         # not without the PIN
        assert _req(base + "/api/v1/wifi/password", {})[0] == 401
        _, r = _req(base + "/api/v1/auth/pin", {"pin": "2468"})
        assert _req(base + "/api/v1/wifi", token=r["token"])[1]["password"] == "Pw23456789ab"
        qr = urllib.request.Request(base + "/api/v1/wifi/qr.svg", headers={"Authorization": f"Bearer {r['token']}"})
        with urllib.request.urlopen(qr, timeout=5) as resp:
            assert resp.headers["Content-Type"] == "image/svg+xml" and resp.read().startswith(b"<svg")
        with pytest.raises(urllib.error.HTTPError) as e:                       # not without the PIN
            urllib.request.urlopen(base + "/api/v1/wifi/qr.svg", timeout=5)
        assert e.value.code == 401
        assert _req(base + "/api/v1/wifi/password", {}, r["token"])[0] == 200
        after = _req(base + "/api/v1/wifi", token=r["token"])[1]
        assert after["password"] != "Pw23456789ab" and len(after["password"]) == 12
        assert after["on_label"] is False                                     # the label's password is kept apart
        assert _req(base + "/api/v1/wifi/password", {"label": True}, r["token"])[1]["on_label"] is True
        assert _req(base + "/api/v1/wifi", token=r["token"])[1]["password"] == "Pw23456789ab"
        # Turned off in settings: the view says so.
        code, _ = _req(base + "/api/v1/settings", {"changes": {"wifi_ap.enabled": False}}, r["token"])
        assert code == 200 and _req(base + "/api/v1/wifi")[1]["enabled"] is False
        assert _req(base + "/api/v1/settings", {"changes": {"wifi_ap.channel": 3}}, r["token"])[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wifi_identity_is_for_the_box_local_client_only(cfg, tmp_path):
    from cerberus import localclient, wifiap
    from cerberus.secretstore import SecretStore
    from cerberus.ui.server import App, serve
    tokf = tmp_path / "local-token"
    tokf.write_text("local-client-token-0123456789abcdef0123")
    tokf.chmod(0o600)
    raw = yaml.safe_load(cfg.read_text()) or {}
    raw["local_client"] = {"token_file": str(tokf)}
    cfg.write_text(yaml.safe_dump(raw))
    assert localclient.token_file(cfg)
    db = Db(str(tmp_path / "i.db"))
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(headers):
        r = urllib.request.Request(base + "/api/v1/wifi/identity", headers={"Host": "127.0.0.1", **headers})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, None
    try:
        good = {"Authorization": "Bearer local-client-token-0123456789abcdef0123"}
        assert get({})[0] == 403                                              # no token
        assert get({"Authorization": "Bearer wrong"})[0] == 403
        assert get({**good, "X-Cerberus-Via": "relay"})[0] == 403             # relayed: never the local client
        code, ident = get(good)
        store = SecretStore.for_config(cfg)
        assert code == 200 and ident["ssid"] == store.get(wifiap.SSID_SECRET)
        assert ident["password"] == store.get(wifiap.LABEL_SECRET) and ident["url"] == "http://10.42.0.1:8088"
        store.set(wifiap.SECRET, "ChangedPass12")                              # the label shows the label's
        assert get(good)[1]["password"] == store.get(wifiap.LABEL_SECRET) != "ChangedPass12"
    finally:
        httpd.shutdown()
        httpd.server_close()
