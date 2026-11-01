"""Heartbeat-triggered reconnect — the half-open-connection recovery.

Regression cover for the 2026-08-08 Starlink gaps: the TCP link went
half-open, paho kept reporting connected, the qos-0 keepalive publishes went
nowhere, and the GX stopped publishing 60s later. Nothing in mqtt_client could
see it — _connected was True the whole time — so recovery had to be driven off
the data feed going quiet. Observed cost before the fix: 2-minute gaps in the
morning, 15-minute gaps overnight.

No sockets: the paho client's transport methods are replaced with recorders.
"""

from __future__ import annotations

import pytest

from cerberus.__main__ import should_force_reconnect
from cerberus.config import MqttConfig
from cerberus.mqtt_client import (
    FORCE_RECONNECT_COOLDOWN_SECONDS,
    CerboMqtt,
)


# ---------------------------------------------------------------------------
# the decision predicate
# ---------------------------------------------------------------------------

def test_forces_when_connected_but_data_stopped():
    """The half-open case: link says up, data says otherwise."""
    assert should_force_reconnect(
        cerbo_online=False, ever_saw_heartbeat=True, mqtt_connected=True) is True


def test_no_force_before_first_heartbeat():
    """At startup the Cerbo reads offline simply because nothing has arrived
    yet. Forcing here would restart the session we just built."""
    assert should_force_reconnect(
        cerbo_online=False, ever_saw_heartbeat=False, mqtt_connected=True) is False


def test_no_force_when_already_disconnected():
    """paho's own reconnect and the resolve watchdog own this case."""
    assert should_force_reconnect(
        cerbo_online=False, ever_saw_heartbeat=True, mqtt_connected=False) is False


def test_no_force_while_data_is_flowing():
    assert should_force_reconnect(
        cerbo_online=True, ever_saw_heartbeat=True, mqtt_connected=True) is False


# ---------------------------------------------------------------------------
# CerboMqtt.force_reconnect
# ---------------------------------------------------------------------------

def _client(monkeypatch, hosts=("venus.local",)):
    cfg = MqttConfig(
        host=hosts[0], hosts=hosts, port=1883, portal_id="abc",
        username="abc", password="pw", keepalive_publish_seconds=30,
        primary_topic="system/0/Batteries", publish_namespace="cerberus",
        ha_discovery_prefix="homeassistant",
    )
    c = CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None)
    calls: list[str] = []
    monkeypatch.setattr(c.client, "loop_stop", lambda: calls.append("loop_stop"))
    monkeypatch.setattr(c.client, "disconnect", lambda: calls.append("disconnect"))
    monkeypatch.setattr(c.client, "loop_start", lambda: calls.append("loop_start"))
    monkeypatch.setattr(c.client, "connect_async",
                        lambda h, p, keepalive=60: calls.append(f"connect:{h}"))
    monkeypatch.setattr(c, "_resolve", lambda *a, **k: "192.168.1.173")
    return c, calls


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 10_000.0}
    monkeypatch.setattr("cerberus.mqtt_client.time.monotonic", lambda: state["t"])
    return state


def test_force_reconnect_rebuilds_the_session(monkeypatch, clock):
    """Order matters: stop the loop, drop the socket, re-point, restart."""
    c, calls = _client(monkeypatch)
    c._connected = True
    assert c.force_reconnect("test") is True
    assert calls == ["loop_stop", "disconnect", "connect:192.168.1.173", "loop_start"]
    assert c.is_connected is False


def test_force_reconnect_reresolves(monkeypatch, clock):
    """A forced reconnect must re-resolve, not reuse the old target — the
    network may have changed underneath us."""
    c, calls = _client(monkeypatch)
    c._target_ip = "192.168.1.67"          # stale home address
    c.force_reconnect("test")
    assert "connect:192.168.1.173" in calls
    assert c._target_ip == "192.168.1.173"


def test_cooldown_suppresses_rapid_refires(monkeypatch, clock):
    """The liveness loop ticks every 5s; without a floor a persistent stall
    would rebuild the session continuously and never let it recover."""
    c, calls = _client(monkeypatch)
    assert c.force_reconnect("first") is True
    calls.clear()
    clock["t"] += 5
    assert c.force_reconnect("second") is False
    assert calls == []


def test_cooldown_expires(monkeypatch, clock):
    c, calls = _client(monkeypatch)
    assert c.force_reconnect("first") is True
    clock["t"] += FORCE_RECONNECT_COOLDOWN_SECONDS + 1
    assert c.force_reconnect("second") is True


def test_first_call_is_never_suppressed(monkeypatch, clock):
    """_last_forced_reconnect starts at 0.0; a monotonic clock reading far
    above the cooldown must not make the first call look recent."""
    c, _ = _client(monkeypatch)
    clock["t"] = 5.0                       # smaller than the cooldown window
    assert c.force_reconnect("first") is True


def test_no_force_when_stopping(monkeypatch, clock):
    c, calls = _client(monkeypatch)
    c._stop.set()
    assert c.force_reconnect("shutting down") is False
    assert calls == []


def test_on_disconnect_defers_while_forcing(monkeypatch, clock):
    """The disconnect force_reconnect itself causes must not schedule a
    competing reconnect."""
    c, _ = _client(monkeypatch)
    scheduled: list[str] = []
    monkeypatch.setattr(c, "_refresh_and_reconnect",
                        lambda: scheduled.append("refresh"))
    c._forcing = True
    c._on_disconnect(c.client, None)
    assert scheduled == []
    assert c.is_connected is False


def test_resolve_failure_does_not_raise(monkeypatch, clock):
    """A forced reconnect during a DNS outage must degrade, not crash the
    liveness loop that called it."""
    c, _ = _client(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("dns down")
    monkeypatch.setattr(c, "_resolve", boom)
    assert c.force_reconnect("test") is False
    assert c._forcing is False   # flag released even on the failure path
