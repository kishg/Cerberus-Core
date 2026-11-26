"""The box answers on the network only to its own names (security review S13: DNS rebinding)."""

import socket

import pytest

from cerberus.ui.server import host_allowed


@pytest.mark.parametrize("addr, host, ok", [
    ("192.168.1.20", "192.168.1.5:8088", True),           # its address
    ("192.168.1.20", "[fd00::5]:8088", True),
    ("192.168.1.20", "cerberus:8088", True),               # a single-label name (hostname, MagicDNS)
    ("192.168.1.20", "cerberus.local", True),              # mDNS
    ("100.70.1.2", "cerberus.tail1234.ts.net", True),      # Tailscale
    ("192.168.1.20", "van.home.arpa", True),
    ("192.168.1.20", "evil.example.com", False),           # a page renamed to the box's address
    ("192.168.1.20", "evil.example.com.", False),
    ("192.168.1.20", "", False),
    ("192.168.1.20", None, False),
    ("127.0.0.1", "abc.box.example", True),                # loopback: phonefix decides (relay, tailscale serve)
])
def test_host_allowed(addr, host, ok):
    assert host_allowed(addr, host) is ok


def test_its_own_hostname():
    assert host_allowed("192.168.1.20", socket.gethostname())
