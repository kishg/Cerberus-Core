"""Host rotation must distinguish "wrong host" from "right host, dead broker".

Regression cover for 2026-09-09. The Cerbo's MQTT broker stalled while the
device itself stayed up — VRM logged continuously throughout, and the mosquitto
bridge (a separate process, socket and client id) failed and recovered within
one second of the collector, so the fault was the broker, not our connection.

The collector then made it four times worse than it needed to be. Rotation is
driven by "not connected", which treated a stalled broker as a bad host: it
rotated 192.168.1.60 -> venus.local, venus.local failed DNS, _resolve rotated
back, and the cycle repeated every ~90s for 50 minutes. .60 was reachable the
whole time. The bridge, which has no rotation logic, simply retried and came
back at the same moment anyway — the rotation bought nothing and cost a
51.7-minute hole in battery_samples.

No sockets are opened here; _port_open is stubbed.
"""

from __future__ import annotations

import pytest

from cerberus.config import MqttConfig
from cerberus.mqtt_client import (
    MAX_ROTATION_SUPPRESSIONS,
    ROTATE_AFTER_ATTEMPTS,
    CerboMqtt,
)

RESERVED_IP = "192.168.1.60"
MDNS_NAME = "venus.local"


def _client(hosts: tuple[str, ...] = (RESERVED_IP, MDNS_NAME)) -> CerboMqtt:
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
    c = CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None)
    c._target_ip = RESERVED_IP
    return c


def _fail_until_rotation_decision(c: CerboMqtt, probe: bool = True) -> None:
    """Drive enough failures to reach the rotate-or-suppress branch once."""
    for _ in range(ROTATE_AFTER_ATTEMPTS):
        c._note_failed_attempt(probe=probe)


def test_reachable_host_is_not_rotated_away_from(monkeypatch):
    """The 09-09 case: broker stalled, port still open. Stay put."""
    c = _client()
    monkeypatch.setattr(c, "_port_open", lambda: True)

    _fail_until_rotation_decision(c)

    assert c.current_host == RESERVED_IP, "rotated away from a reachable host"
    assert c._rotation_suppressions == 1


def test_unreachable_host_still_rotates(monkeypatch):
    """The case rotation exists for — network moved, host genuinely gone."""
    c = _client()
    monkeypatch.setattr(c, "_port_open", lambda: False)

    _fail_until_rotation_decision(c)

    assert c.current_host == MDNS_NAME, "failed to rotate off an unreachable host"
    assert c._rotation_suppressions == 0


def test_suppression_is_capped(monkeypatch):
    """A listening-but-permanently-broken broker must still cost a rotation.

    Otherwise this trades the rotation loop for a different stuck state.
    """
    c = _client()
    monkeypatch.setattr(c, "_port_open", lambda: True)

    for _ in range(MAX_ROTATION_SUPPRESSIONS):
        _fail_until_rotation_decision(c)
    assert c.current_host == RESERVED_IP
    assert c._rotation_suppressions == MAX_ROTATION_SUPPRESSIONS

    _fail_until_rotation_decision(c)
    assert c.current_host == MDNS_NAME, "cap did not release the rotation"


def test_successful_connect_resets_suppressions(monkeypatch):
    """Recovery must clear the budget, or a later stall inherits a spent one."""
    c = _client()
    monkeypatch.setattr(c, "_port_open", lambda: True)
    _fail_until_rotation_decision(c)
    assert c._rotation_suppressions == 1

    c.client.subscribe = lambda *a, **k: None
    c.client.publish = lambda *a, **k: None
    c._on_connect(c.client, None, {}, 0)

    assert c._rotation_suppressions == 0
    assert c._attempts_since_last_success == 0


def test_probe_is_off_by_default(monkeypatch):
    """paho callback threads must not block on a 2s socket connect."""
    c = _client()
    probed = []
    monkeypatch.setattr(c, "_port_open", lambda: probed.append(1) or True)

    _fail_until_rotation_decision(c, probe=False)

    assert probed == [], "probed from a non-watchdog caller"
    assert c.current_host == MDNS_NAME, "default path should rotate as before"


def test_single_host_never_rotates_or_probes(monkeypatch):
    """Nothing to rotate to — the probe is pointless work."""
    c = _client(hosts=(RESERVED_IP,))
    probed = []
    monkeypatch.setattr(c, "_port_open", lambda: probed.append(1) or True)

    _fail_until_rotation_decision(c)

    assert c.current_host == RESERVED_IP
    assert probed == []
