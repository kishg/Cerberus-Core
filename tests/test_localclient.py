"""The box's trusted local client (cerberus.localclient): links the box to a cloud service and sets
storage mode, only from the box itself, only with its token file's token, and nothing else."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod, secretstore
from cerberus.db import Db
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
TOKEN = "t" * 40
LINK = {"cloud_token": "iots_" + "x" * 30,
        "health_report": {"enabled": True, "target_url": "https://cloud.example/api/v1/cerberus/health"},
        "sync": {"enabled": True, "target_url": "https://cloud.example/api/v1/cerberus/sync", "tables": "beta",
                 "share_position": False}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    cfgp = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfgp)
    tok = tmp_path / "local-client.token"
    tok.write_text(TOKEN + "\n")
    os.chmod(tok, 0o640)
    cfg_mod.setup_path(cfgp).write_text(yaml.safe_dump({"local_client": {"token_file": str(tok)}}))
    d = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfgp), d._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfgp, tok, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    d.close()


def _post(port, path, body, token=TOKEN, **headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1" + path, body=json.dumps(body),
              headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}),
                       **headers})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def _setup(cfgp):
    return yaml.safe_load(cfg_mod.setup_path(cfgp).read_text())


def test_link_stores_the_token_and_turns_reports_on(box):
    cfgp, _, port = box
    code, r = _post(port, "/cloud/link", LINK)
    assert code == 200 and r["linked"] is True
    assert secretstore.SecretStore.for_config(cfgp).get("cloud_token") == LINK["cloud_token"]
    s = _setup(cfgp)
    assert s["health_report"]["enabled"] is True and s["sync"]["tables"] == "beta"
    assert s["sync"]["share_position"] is False and s["local_client"]        # existing setup kept
    loaded = cfg_mod.load(str(cfgp))                                          # and it all still loads
    assert loaded.sync.enabled and loaded.health_report.enabled
    assert "x" * 30 not in cfg_mod.setup_path(cfgp).read_text()               # the token isn't in setup.yaml

    code, r = _post(port, "/cloud/unlink", {"x": 1})
    assert code == 200 and r["linked"] is False
    assert secretstore.SecretStore.for_config(cfgp).get("cloud_token") is None
    assert _setup(cfgp)["sync"]["enabled"] is False and _setup(cfgp)["health_report"]["enabled"] is False


def test_only_the_local_client_on_the_box(box):
    cfgp, tok, port = box
    assert _post(port, "/cloud/link", LINK, token=None)[0] == 403
    assert _post(port, "/cloud/link", LINK, token="w" * 40)[0] == 403
    # Proxied to the box (tailscale serve, or a relay agent forwarding a remote request): never the
    # local client, even with the right token.
    assert _post(port, "/cloud/link", LINK, **{"X-Forwarded-For": "100.64.0.9"})[0] == 403
    assert _post(port, "/cloud/link", LINK, Host="abc.box.example")[0] == 403
    # A token file anyone can read is no secret: ignored.
    os.chmod(tok, 0o644)
    assert _post(port, "/cloud/link", LINK)[0] == 403
    os.chmod(tok, 0o640)
    assert _post(port, "/cloud/link", LINK)[0] == 200
    # It can't change anything else.
    assert _post(port, "/settings", {"changes": {"battery.pack_count": 2}})[0] == 401
    assert secretstore.SecretStore.for_config(cfgp).get("cloud_token")


def test_link_refuses_what_it_may_not_set(box):
    cfgp, _, port = box
    for bad in ({**LINK, "cloud_token": "short"},
                {**LINK, "sync": {**LINK["sync"], "target_url": "http://cloud.example/sync"}},
                {**LINK, "sync": {**LINK["sync"], "auth_token": "sneaky"}},
                {**LINK, "sync": {**LINK["sync"], "tables": ["not_a_table"]}},
                {**LINK, "health_report": "on"}):
        code, r = _post(port, "/cloud/link", bad)
        assert code == 400, bad
    assert secretstore.SecretStore.for_config(cfgp).get("cloud_token") is None       # nothing half-done
    assert "sync" not in _setup(cfgp)


def test_without_a_token_file_there_is_no_local_client(box):
    cfgp, _, port = box
    cfg_mod.setup_path(cfgp).write_text("")
    assert _post(port, "/cloud/link", LINK)[0] == 403


def test_storage_mode_from_the_local_client(box):
    _, _, port = box
    code, r = _post(port, "/storage", {"on": True, "until": "2027-03-01"})
    assert code == 200 and r["storage"]["on"] is True
    assert _post(port, "/storage", {"on": False}, token="w" * 40)[0] == 401
