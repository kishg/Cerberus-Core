"""DNS re-resolution — cached-IP expiry and the disconnected watchdog.

Regression cover for the 2026-08-07 van outage: the collector moved from the
home LAN to the van's Starlink, mDNS briefly failed, and _resolve() handed
paho the cached *home* IP. paho then retried that address forever without
re-resolving, and because a failing connect fires neither on_connect nor
on_disconnect, nothing logged and nothing recovered. Data was dead for 3.5h
while HA kept rendering retained values.

No sockets are opened here — getaddrinfo and the clock are stubbed, and the
paho client's connect_async is replaced with a recorder.
"""

from __future__ import annotations

import socket

import pytest

from cerberus.config import MqttConfig
from cerberus.mqtt_client import (
    CACHED_IP_TTL_SECONDS,
    CerboMqtt,
)

HOME_IP = "192.168.1.67"
VAN_IP = "192.168.1.173"


def _client(hosts: tuple[str, ...] = ("venus.local",)) -> CerboMqtt:
    cfg = MqttConfig(
        host=hosts[0],
        hosts=hosts,
        port=1883,
        portal_id="abc",
        username="abc",
        password="pw",
        keepalive_publish_seconds=30,
        primary_topic="system/0/Batteries",
        publish_namespace="cerberus",
        ha_discovery_prefix="homeassistant",
    )
    # construct only — never call .start(), so no threads and no network.
    return CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None)


@pytest.fixture
def clock(monkeypatch):
    """Controllable monotonic clock. _resolve stamps the cache with it."""
    state = {"t": 1000.0}
    monkeypatch.setattr("cerberus.mqtt_client.time.monotonic", lambda: state["t"])
    return state


def _addrinfo(ip: str, family=socket.AF_INET):
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 1883))]


def _stub_dns(monkeypatch, result):
    """result: an ip string to resolve to, or an exception to raise."""
    def fake(host, port, **kw):
        if isinstance(result, Exception):
            raise result
        return _addrinfo(result)
    monkeypatch.setattr(socket, "getaddrinfo", fake)


GAI_FAIL = socket.gaierror(8, "nodename nor servname provided, or not known")


# ---------------------------------------------------------------------------
# cached-IP expiry
# ---------------------------------------------------------------------------

def test_cache_reused_for_brief_dns_hiccup(monkeypatch, clock):
    """A momentary mDNS blip should NOT drop the connection target."""
    c = _client()
    _stub_dns(monkeypatch, VAN_IP)
    assert c._resolve() == VAN_IP

    clock["t"] += 10  # well inside the TTL
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._resolve() == VAN_IP


def test_cache_discarded_past_ttl_returns_hostname(monkeypatch, clock):
    """Past the TTL the cached IP is assumed wrong, not merely stale.

    Returning the hostname is the point: paho re-resolves a name on every
    reconnect, so the collector self-heals once DNS returns. Returning the
    old IP is what wedged it for 3.5 hours.
    """
    c = _client()
    _stub_dns(monkeypatch, HOME_IP)
    assert c._resolve() == HOME_IP

    clock["t"] += CACHED_IP_TTL_SECONDS + 1
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._resolve() == "venus.local"
    assert "venus.local" not in c._cached_ip  # evicted, not just skipped


def test_ttl_boundary_is_inclusive(monkeypatch, clock):
    c = _client()
    _stub_dns(monkeypatch, VAN_IP)
    c._resolve()
    clock["t"] += CACHED_IP_TTL_SECONDS  # exactly at the limit
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._resolve() == VAN_IP


def test_no_cache_and_dns_down_returns_hostname(monkeypatch, clock):
    c = _client()
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._resolve() == "venus.local"


def test_all_hosts_failing_dns_terminates(monkeypatch, clock):
    """Every host failing to resolve must not recurse forever.

    _resolve rotates onto the next host on failure; without a bound that
    walks the ring endlessly and blows the stack.
    """
    c = _client(("a.local", "b.local", "c.local"))
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._resolve() in {"a.local", "b.local", "c.local"}


# ---------------------------------------------------------------------------
# the watchdog
# ---------------------------------------------------------------------------

def _recording_client(monkeypatch, hosts=("venus.local",)):
    c = _client(hosts)
    calls: list[tuple] = []
    monkeypatch.setattr(c.client, "connect_async",
                        lambda h, p, keepalive=60: calls.append((h, p)))
    return c, calls


def test_watchdog_noop_while_connected(monkeypatch, clock):
    c, calls = _recording_client(monkeypatch)
    c._connected = True
    c._target_ip = HOME_IP
    _stub_dns(monkeypatch, VAN_IP)
    assert c._watchdog_tick() is False
    assert calls == []


def test_watchdog_retargets_after_network_move(monkeypatch, clock):
    """The actual van regression, end to end through the watchdog.

    Pointed at the home IP, disconnected, and DNS now answers with the van
    address — the watchdog must move paho onto it with no callback required.
    """
    c, calls = _recording_client(monkeypatch)
    c._connected = False
    c._target_ip = HOME_IP
    _stub_dns(monkeypatch, VAN_IP)

    assert c._watchdog_tick() is True
    assert calls == [(VAN_IP, 1883)]
    assert c._target_ip == VAN_IP


def test_watchdog_quiet_when_target_unchanged(monkeypatch, clock):
    """Cerbo simply unreachable — don't churn connect_async every 30s."""
    c, calls = _recording_client(monkeypatch)
    c._connected = False
    c._target_ip = VAN_IP
    _stub_dns(monkeypatch, VAN_IP)
    assert c._watchdog_tick() is False
    assert calls == []


def test_watchdog_escapes_stale_cache_via_ttl(monkeypatch, clock):
    """Full sequence: cached home IP, DNS down, then DNS returns the van IP."""
    c, calls = _recording_client(monkeypatch)
    _stub_dns(monkeypatch, HOME_IP)
    c._retarget(c._resolve())
    calls.clear()
    c._connected = False

    # DNS goes away; past the TTL we stop insisting on the home address.
    clock["t"] += CACHED_IP_TTL_SECONDS + 1
    _stub_dns(monkeypatch, GAI_FAIL)
    assert c._watchdog_tick() is True
    assert calls[-1] == ("venus.local", 1883)

    # mDNS comes back on the new network.
    _stub_dns(monkeypatch, VAN_IP)
    assert c._watchdog_tick() is True
    assert calls[-1] == (VAN_IP, 1883)


def test_connect_failure_clears_connected_flag(monkeypatch, clock):
    """A broker-refused connect must leave the watchdog armed."""
    c = _client()
    c._connected = True
    c._on_connect(c.client, None, {}, 5)  # 5 = not authorised
    assert c._connected is False
