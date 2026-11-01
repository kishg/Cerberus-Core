"""The addresses the Settings screen offers for opening Cerberus on a phone."""

from __future__ import annotations

import pytest

from cerberus.ui import access

STATUS = {"BackendState": "Running",
          "Self": {"DNSName": "vanbox.tail1234.ts.net.", "TailscaleIPs": ["100.1.2.3"]}}
SERVE = {"TCP": {"443": {"HTTPS": True}},
         "Web": {"vanbox.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8088"}}}}}


def test_serving_https():
    assert access.tailscale_https(8088, STATUS, SERVE) == {
        "name": "vanbox.tail1234.ts.net", "url": "https://vanbox.tail1234.ts.net/",
        "serving": True, "command": None}


@pytest.mark.parametrize("serve", [None, {}, {"Web": {"vanbox.tail1234.ts.net:443": {
    "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}])
def test_not_serving_this_ui_says_how(serve):
    t = access.tailscale_https(8088, STATUS, serve)
    assert t["url"] is None and t["command"] == "tailscale serve --bg 8088"


@pytest.mark.parametrize("status", [None, {"BackendState": "NeedsLogin", "Self": STATUS["Self"]},
                                    {"BackendState": "Running", "Self": {}}])
def test_no_tailscale(status):
    assert access.tailscale_https(8088, status, SERVE) is None


def test_view_without_the_cli(monkeypatch):
    monkeypatch.setattr(access, "_tailscale", lambda: None)
    monkeypatch.setattr(access, "_cache", None)
    v = access.view(8088)
    assert v["tailscale"] is None and (v["lan"] is None or v["lan"].endswith(":8088/"))


def test_view_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(access, "_cache", None)
    monkeypatch.setattr(access, "_tailscale", lambda: "/x/tailscale")
    monkeypatch.setattr(access, "_json", lambda cmd: calls.append(cmd) or (STATUS if "status" == cmd[1] else SERVE))
    assert access.view(8088, now=0)["tailscale"]["url"] == "https://vanbox.tail1234.ts.net/"
    access.view(8088, now=30)
    assert len(calls) == 2
    access.view(8088, now=61)
    assert len(calls) == 4


# --- is a Tailscale peer on the van's network? -------------------------------------

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
