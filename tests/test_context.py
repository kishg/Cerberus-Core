"""GET /api/v1/context: how the browser reaches the box, so the UI can say where you are."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from cerberus.db import Db
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture
def port(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    cfgp = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfgp)
    d = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfgp), d._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    d.close()


def _context(port, **headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/v1/context", headers=headers)
    return json.loads(c.getresponse().read())


def test_at_the_box(port):
    assert _context(port, Host="localhost:8088") == {"via": "box"}


def test_through_a_relay_with_the_way_back(port):
    c = _context(port, Host="abc.box.example", **{"X-Cerberus-Via": "relay",
                                                 "X-Cerberus-Return": "https://app.example/#/place/1",
                                                 "X-Cerberus-Place": "Test Van"})
    assert c == {"via": "relay", "return_url": "https://app.example/#/place/1", "place": "Test Van"}


def test_only_an_https_way_back(port):
    for bad in ("javascript:alert(1)", "http://app.example/", "//app.example/"):
        c = _context(port, **{"X-Cerberus-Via": "relay", "X-Cerberus-Return": bad})
        assert c["via"] == "relay" and c["return_url"] is None


def test_relay_headers_mean_nothing_from_the_network_side(port):
    # Over Tailscale serve (proxied), a Return header doesn't make it a relay.
    c = _context(port, Host="van.tailnet.example", **{"X-Cerberus-Return": "https://evil.example/"})
    assert c == {"via": "tailscale"}


def test_the_brand_fonts_come_from_the_box(port):
    for name in ("font-inter.woff2", "font-space-grotesk.woff2", "font-jetbrains-mono.woff2"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/" + name)
        r = c.getresponse()
        assert (r.status, r.getheader("Content-Type"), r.read()[:4]) == (200, "font/woff2", b"wOF2")
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/FONTS-LICENSE.txt")
    assert b"SIL Open Font License" in c.getresponse().read()
