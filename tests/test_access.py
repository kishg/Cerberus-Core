"""Settings > Open Cerberus: the van-network address, the box's tailnet, and support access.

Remote access is a Cerberus Cloud feature (Core #52), so no Tailscale address is offered. Who can
reach the box remotely is shown (Core #48): its tailnet, and "Support access: on" when that is the
configured support provider's tailnet.
"""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from cerberus.config import SupportConfig
from cerberus.db import Db
from cerberus.ui import access
from cerberus.ui.server import App, serve

ROOT = Path(__file__).resolve().parent.parent
# Made-up tailnets: the owner's own, and a support provider's.
OWN = {"BackendState": "Running", "CurrentTailnet": {"Name": "owner.example"},
       "Self": {"DNSName": "vanbox.tail1234.ts.net.", "TailscaleIPs": ["100.1.2.3"]}}
SUPPORTED = {"BackendState": "Running", "CurrentTailnet": {"Name": "acme-support.example"},
             "Self": {"DNSName": "beta-box-1.acme-support.example."}}
SUPPORT = SupportConfig(tailnet="acme-support.example", provider="Acme Support", contact="help@acme.example")


@pytest.mark.parametrize("status", [None, {}, {"BackendState": "NeedsLogin", "Self": OWN["Self"]},
                                    {"BackendState": "Running", "Self": {}}])
def test_not_on_a_tailnet(status):
    assert access.tailnet(status) is None
    assert access.support(access.tailnet(status), SUPPORT) is None


def test_on_the_owners_tailnet():
    on = access.tailnet(OWN)
    assert on == {"tailnet": "owner.example", "name": "vanbox.tail1234.ts.net"}
    assert access.support(on, SUPPORT) is None            # not the support tailnet
    assert access.support(on, None) is None                # no support: section


def test_tailnet_from_the_dns_name_when_the_cli_doesnt_say():
    st = {"BackendState": "Running", "Self": {"DNSName": "box.tail99.ts.net."}}
    assert access.tailnet(st) == {"tailnet": "tail99.ts.net", "name": "box.tail99.ts.net"}


def test_on_the_support_tailnet():
    s = access.support(access.tailnet(SUPPORTED), SUPPORT)
    assert s["on"] is True and s["provider"] == "Acme Support" and s["contact"] == "help@acme.example"
    assert "sudo tailscale logout" in s["end"]
    assert access.support(access.tailnet(SUPPORTED), SupportConfig()) is None   # section absent


def test_view_offers_only_the_van_network(monkeypatch):
    monkeypatch.setattr(access, "_tailscale", lambda: None)
    monkeypatch.setattr(access, "_cache", None)
    v = access.view(8088)
    assert set(v) == {"lan", "tailnet", "support"} and v["tailnet"] is None and v["support"] is None
    assert v["lan"] is None or v["lan"].endswith(":8088/")


def test_status_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_tailscale", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(access, "_json", lambda cmd: calls.append(cmd) or SUPPORTED)
    monkeypatch.setattr(access, "_cache", None)
    assert access.remote(SUPPORT, now=0)["support"]["on"] is True
    access.remote(SUPPORT, now=30)
    assert len(calls) == 1
    access.remote(SUPPORT, now=61)
    assert len(calls) == 2


def test_access_and_health_apis_say_when_support_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    monkeypatch.setattr(access, "_tailscale", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(access, "_json", lambda cmd: SUPPORTED)
    monkeypatch.setattr(access, "_cache", None)
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["support"] = {"tailnet": "acme-support.example", "provider": "Acme Support", "contact": "help@acme.example"}
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(raw))
    db = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfgp), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/v1" + path)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        code, a = get("/access")
        assert code == 200 and a["support"]["on"] and "tailscale" not in a
        code, h = get("/health")
        assert code == 200 and h["remote"]["support"]["provider"] == "Acme Support"
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


# --- is a Tailscale peer on the van's network? (phone location, until the rest of #52) ---

import ipaddress

LAN = [ipaddress.ip_network("192.168.1.0/24")]
PEERS = {"BackendState": "Running", "Peer": {
    "phone": {"TailscaleIPs": ["100.64.0.10", "fd7a::1"], "CurAddr": "192.168.1.61:41641"},
    "away": {"TailscaleIPs": ["100.65.1.1"], "CurAddr": "73.12.4.9:41641"},
    "relayed": {"TailscaleIPs": ["100.65.1.2"], "CurAddr": "", "Relay": "nyc"},
}}


@pytest.mark.parametrize("ip, want", [("100.64.0.10", "192.168.1.61:41641"), ("100.65.1.1", None),
                                      ("100.65.1.2", None), ("100.99.9.9", None)])
def test_peer_on_lan(ip, want):
    assert access.peer_on_lan(ip, networks=LAN, status=PEERS) == want
