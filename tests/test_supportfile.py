"""The support file (Core #44): one download, never a secret, position only when ticked.

All values here are made up."""

from __future__ import annotations

import io
import json
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
import yaml

from cerberus import envfile, supportfile
from cerberus.db import Db
from cerberus.events import Severity, make
from cerberus.location import KV_POSITION
from cerberus.secretstore import CLOUD_TOKEN, SYNC_TOKEN, SecretStore
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
NOW = 1793470000

# Planted secrets: none of these may appear anywhere in the file.
CERBO_PASSWORD = "Cerbo-Pw-Zq81!x"
VRM_TOKEN = "vrmTOKEN0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c"
GOVEE_KEY = "govee-key-77f1c2e9-aaaa-bbbb"
SYNC_TOKEN_VALUE = "sync_tok_Jx9QpLr2Vw"
CLOUD_TOKEN_VALUE = "cloudTok_4ZtYk8Hn"
HEALTH_TOKEN_IN_CONFIG = "hr-tok-Pq7Ws3Ld"
CONFIG_PASSWORD = "cfg-pw-Mn5Kd0"
WIFI_PASSWORD = "wifi-pass-Tg6Hu2"
PIN = "97531"
LOG_TOKEN = "logged-bearer-Rf4Ed3Ws2Qa1"
# Made-up position, distinctive enough to search for.
LAT, LON = 12.3456, -98.7654
ZIP = "00042"


@pytest.fixture
def box(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    raw = yaml.safe_load(cfg.read_text())
    raw["mqtt"]["password"] = CONFIG_PASSWORD              # a Cerbo password in config, by mistake
    raw["health_report"]["auth_token"] = HEALTH_TOKEN_IN_CONFIG
    raw["weather"].update({"latitude": LAT, "longitude": LON, "zip_code": ZIP})
    cfg.write_text(yaml.safe_dump(raw))
    (tmp_path / "setup.yaml").write_text(yaml.safe_dump({"sync": {"auth_token": SYNC_TOKEN_VALUE},
                                                         "solar": {"array_w": 400}}))
    env = envfile.path_for(cfg)
    env.write_text(f"CERBERUS_MQTT_PASSWORD='{CERBO_PASSWORD}'\nVRM_TOKEN='{VRM_TOKEN}'\n"
                   f"GOVEE_API_KEY='{GOVEE_KEY}'\n")
    store = SecretStore.for_config(cfg)
    store.set(SYNC_TOKEN, SYNC_TOKEN_VALUE)
    store.set(CLOUD_TOKEN, CLOUD_TOKEN_VALUE)
    store.set("wifi_ap_password", WIFI_PASSWORD)
    auth.set_pin(cfg, PIN)
    sessions = auth.Sessions(cfg)
    session_token = sessions.login(PIN, "127.0.0.1")

    db = Db(str(tmp_path / "c.db"))
    db.set_kv("device.id", "11111111-2222-4333-8444-555555555555")
    db.set_kv(KV_POSITION, json.dumps({"lat": LAT, "lon": LON, "ts": NOW, "source": "gps"}))
    db.insert_event(make("cerbo_offline", Severity.WARN,
                         {"host": "192.0.2.10", "password": CERBO_PASSWORD, "lat": LAT, "lon": LON,
                          "note": f"token={VRM_TOKEN}"}, ts=NOW - 60))
    logs = tmp_path / "soak"
    logs.mkdir()
    (logs / "cerberus.log").write_text(
        "2026-01-01 00:00:00 INFO cerberus starting\n"
        f"2026-01-01 00:00:01 INFO sync POST Authorization: Bearer {LOG_TOKEN}\n"
        f"2026-01-01 00:00:02 INFO vrm x-authorization: Token {VRM_TOKEN}\n"
        f"2026-01-01 00:00:03 DEBUG mqtt connect password={CERBO_PASSWORD}\n"
        f"2026-01-01 00:00:04 INFO position {LAT}, {LON} from gps\n"
        "2026-01-01 00:00:05 INFO https://user:hunter2hunter2@sync.example.invalid/x\n")
    return {"cfg": cfg, "db": db, "session": session_token, "tmp": tmp_path}


SECRETS = (CERBO_PASSWORD, VRM_TOKEN, GOVEE_KEY, SYNC_TOKEN_VALUE, CLOUD_TOKEN_VALUE, HEALTH_TOKEN_IN_CONFIG,
           CONFIG_PASSWORD, WIFI_PASSWORD, LOG_TOKEN, "hunter2hunter2")


def _build(box, **kw):
    return supportfile.build(box["cfg"], box["db"]._path, now=NOW, log_dir=None, **kw)


def _all_text(files):
    return "\n".join(files.values())


def test_no_secret_appears_anywhere(box):
    text = _all_text(_build(box))
    for s in SECRETS:
        assert s not in text, s
    pin_file = json.loads(auth.pin_path(box["cfg"]).read_text())
    assert pin_file["hash"] not in text and pin_file["salt"] not in text
    assert PIN not in text
    assert box["session"] not in text
    for h in json.loads(auth.sessions_path(box["cfg"]).read_text()).get("sessions", {}):
        assert h not in text
    assert "[removed]" in text


def test_no_secret_even_with_position_included(box):
    text = _all_text(_build(box, include_position=True))
    for s in SECRETS:
        assert s not in text, s


def test_no_position_unless_ticked(box):
    files = _build(box)
    text = _all_text(files)
    for v in (str(LAT), str(LON), ZIP):
        assert v not in text, v
    assert "position.json" not in files
    assert "latitude" not in files["config.yaml"]
    assert "[position removed]" in files["logs/cerberus.log"]
    ticked = _build(box, include_position=True)
    t = _all_text(ticked)
    assert str(LAT) in t and str(LON) in t
    assert json.loads(ticked["position.json"])["lat"] == LAT


def test_contents(box):
    files = _build(box)
    for name in ("status.md", "health.json", "findings.json", "events.json", "box.json",
                 "config.yaml", "setup.yaml", "logs/cerberus.log", "README.txt"):
        assert name in files, name
    for name in ("health.json", "findings.json", "events.json", "box.json"):
        json.loads(files[name])                       # still valid JSON after scrubbing
    box_info = json.loads(files["box.json"])
    assert box_info["device_id"] == "11111111-2222-4333-8444-555555555555"
    assert box_info["core_version"]
    ev = json.loads(files["events.json"])
    assert ev[0]["type"] == "cerbo_offline" and ev[0]["detail"]["password"] == "[removed]"
    assert ev[0]["detail"]["host"] == "192.0.2.10"     # addresses help support; not secrets
    cfg = yaml.safe_load(files["config.yaml"])
    assert cfg["mqtt"]["password"] == "[removed]" and cfg["mqtt"]["port"] == 1883
    assert yaml.safe_load(files["setup.yaml"])["solar"]["array_w"] == 400
    assert "Metric" in files["status.md"]
    assert "cerberus starting" in files["logs/cerberus.log"]


def test_works_without_a_database(box, tmp_path):
    files = supportfile.build(box["cfg"], str(tmp_path / "missing.db"), now=NOW, log_dir=None)
    assert "could not be read" in files["status.md"]
    assert "config.yaml" in files and "box.json" in files


def test_box_reads_each_services_own_log_file(box, tmp_path):
    logs = tmp_path / "var-log-cerberus"
    logs.mkdir()
    (logs / "cerberus-ui.log").write_text("".join(f"ui line {i}\n" for i in range(400)))
    (logs / "cerberus-ble.log").write_text(f"ble line api_key={GOVEE_KEY}\n")
    # Just rotated: the current file is short, the rest comes from .1.
    (logs / "cerberus.log.1").write_text("".join(f"old {i}\n" for i in range(10)))
    (logs / "cerberus.log").write_text("new 0\nnew 1\n")
    files = supportfile.build(box["cfg"], box["db"]._path, now=NOW, log_dir=str(logs))
    ui = files["logs/cerberus-ui.log"].splitlines()
    assert len(ui) == supportfile.LOG_LINES and ui[-1] == "ui line 399" and ui[0] == "ui line 100"
    assert files["logs/cerberus.log"].splitlines() == [f"old {i}" for i in range(10)] + ["new 0", "new 1"]
    assert "couldn't be read" in files["logs/cerberus-wifi.log"]       # not written yet
    assert GOVEE_KEY not in _all_text(files)
    assert "soak" not in " ".join(files) and "cerberus starting" not in _all_text(files)   # not the Mac's


def test_log_dir_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CERBERUS_LOG_DIR", str(tmp_path))
    assert supportfile.log_dir() == str(tmp_path)
    monkeypatch.delenv("CERBERUS_LOG_DIR")
    monkeypatch.setattr(supportfile.sys, "platform", "linux")
    assert supportfile.log_dir() == supportfile.BOX_LOG_DIR
    monkeypatch.setattr(supportfile.sys, "platform", "darwin")
    assert supportfile.log_dir() is None


@pytest.mark.parametrize("name, secret", [
    ("password", True), ("auth_token", True), ("CERBERUS_MQTT_PASSWORD", True), ("api_key", True),
    ("GOVEE_API_KEY", True), ("apiKey", True), ("setup_code", True), ("pin", True), ("x-authorization", True),
    ("sessionCookie", True), ("wifi_psk", True),
    ("pin_set", False), ("key", False), ("port", False), ("username", False), ("primary_topic", False),
    ("keepalive_publish_seconds", False), ("share_position", False), ("pack_addresses", False),
])
def test_is_secret_key(name, secret):
    assert supportfile.is_secret_key(name) is secret


def test_redact_keeps_booleans_and_drops_position():
    out = supportfile.redact({"pin_set": True, "token": "abc", "auth": {"token": "x"}, "enabled": False,
                              "lat": 1.0, "detail": json.dumps({"password": "p", "lon": 2.0, "v": 13.2}),
                              "token_enabled": False})
    assert out["pin_set"] is True and out["token"] == "[removed]" and out["auth"] == "[removed]"
    assert "lat" not in out and out["token_enabled"] is False
    assert json.loads(out["detail"]) == {"password": "[removed]", "v": 13.2}


@pytest.mark.parametrize("line, gone", [
    ("Authorization: Bearer abcDEF123456", "abcDEF123456"),
    ('{"auth_token": "s3cr3t-value"}', "s3cr3t-value"),
    ("GOVEE_API_KEY=zz-key-value", "zz-key-value"),
    ("POST /login?pin=24680 ok", "24680"),
    ("mqtts://user:pw-abc123@broker.example.invalid", "pw-abc123"),
    ("token 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c", "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"),
])
def test_scrub_text_patterns(line, gone):
    assert gone not in supportfile.scrub_text(line)


def test_scrub_text_keeps_ordinary_lines():
    line = ("2026-01-01 INFO device id 11111111-2222-4333-8444-555555555555 pack 1 13.31 V "
            "pin_set: true host 192.0.2.10 cell_imbalance_cross_pack_mv")
    assert supportfile.scrub_text(line) == line


# --- the server ----------------------------------------------------------------------------

@pytest.fixture
def server(box):
    httpd = serve(App.from_config(str(box["cfg"]), box["db"]._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url, token=None):
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_support_file_needs_a_session(server):
    assert _get(server + "/api/v1/support/file")[0] == 401
    assert _get(server + "/api/v1/support/preview")[0] == 401
    assert _get(server + "/api/v1/support/file", "not-a-session")[0] == 401


def test_support_file_download(server, box):
    code, headers, body = _get(server + "/api/v1/support/file", box["session"])
    assert code == 200 and headers["Content-Type"] == "application/zip"
    assert 'filename="cerberus-support-11111111-' in headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
        text = "\n".join(z.read(n).decode() for n in names)
    assert "cerberus-support/status.md" in names and "cerberus-support/logs/cerberus.log" in names
    for s in SECRETS + (PIN, box["session"]):
        assert s not in text, s
    assert str(LAT) not in text and "cerberus-support/position.json" not in names
    code, _, body = _get(server + "/api/v1/support/file?position=1", box["session"])
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        assert "cerberus-support/position.json" in z.namelist()


def test_support_preview(server, box):
    code, _, body = _get(server + "/api/v1/support/preview", box["session"])
    p = json.loads(body)
    assert code == 200 and [s["name"] for s in p["sections"]][0] == "status.md"
    assert p["position_included"] is False and "[removed]" in p["config"]["config.yaml"]
    for s in SECRETS:
        assert s not in body.decode()
    assert str(LAT) not in p["config"]["config.yaml"]
