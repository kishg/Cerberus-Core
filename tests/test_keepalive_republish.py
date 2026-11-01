"""Keepalive payloads — full republish only on connect and every
full_republish_seconds, suppress-republish otherwise.

The empty keepalive makes the GX resend its whole tree (~2050 messages,
measured 2026-10-14). Sent every 30 s it was ~80% of the Cerbo's WiFi traffic.

No sockets: the paho client's publish is replaced with a recorder.
"""

from __future__ import annotations

from cerberus import mqtt_client
from cerberus.config import MqttConfig
from cerberus.mqtt_client import SUPPRESS_REPUBLISH_PAYLOAD, CerboMqtt


def _client(monkeypatch, full_republish_seconds=300):
    cfg = MqttConfig(
        host="venus.local", hosts=("venus.local",), port=1883, portal_id="abc",
        username="abc", password="pw", keepalive_publish_seconds=30,
        primary_topic="system/0/Batteries", publish_namespace="cerberus",
        ha_discovery_prefix="homeassistant",
        full_republish_seconds=full_republish_seconds,
    )
    noop = lambda *a, **k: None
    c = CerboMqtt(cfg, on_batteries=noop, on_heartbeat=noop)
    sent: list[bytes] = []
    monkeypatch.setattr(c.client, "publish",
                        lambda topic, payload=None, qos=0, retain=False:
                        sent.append(payload) if topic == "R/abc/keepalive" else None)
    return c, sent


def _run_loop(c, monkeypatch, ticks, clock):
    """Drive _keepalive_loop for `ticks` periods on a fake monotonic clock."""
    state = {"n": 0}

    def fake_wait(period):
        if state["n"] >= ticks:
            return True
        state["n"] += 1
        clock[0] += period
        return False

    monkeypatch.setattr(c._stop, "wait", fake_wait)
    c._keepalive_loop()


def test_default_keepalive_is_full_republish(monkeypatch):
    """The connect path calls _publish_keepalive() bare — it must stay a full
    republish, or rarely-changing topics never arrive after a reconnect."""
    c, sent = _client(monkeypatch)
    c._publish_keepalive()
    assert sent == [b""]


def test_periodic_keepalives_suppress_between_full_republishes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mqtt_client.time, "monotonic", lambda: clock[0])
    c, sent = _client(monkeypatch, full_republish_seconds=300)
    c._publish_keepalive()                     # connect at t=1000
    _run_loop(c, monkeypatch, ticks=20, clock=clock)   # t=1030 .. 1600
    # connect, then 30-s ticks: full republish due at t=1300 and t=1600
    full = [i for i, p in enumerate(sent) if p == b""]
    assert full == [0, 10, 20]
    assert all(p == SUPPRESS_REPUBLISH_PAYLOAD
               for i, p in enumerate(sent) if i not in full)


def test_reconnect_resets_the_full_republish_timer(monkeypatch):
    """A reconnect's full republish counts — no second dump right after it."""
    clock = [1000.0]
    monkeypatch.setattr(mqtt_client.time, "monotonic", lambda: clock[0])
    c, sent = _client(monkeypatch, full_republish_seconds=300)
    c._publish_keepalive()                     # t=1000
    clock[0] = 1290.0
    c._publish_keepalive()                     # reconnect at t=1290
    sent.clear()
    _run_loop(c, monkeypatch, ticks=1, clock=clock)    # t=1320: only 30 s later
    assert sent == [SUPPRESS_REPUBLISH_PAYLOAD]
