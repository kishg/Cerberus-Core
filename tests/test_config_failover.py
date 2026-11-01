"""Multi-host MQTT failover — config parsing + rotation state machine.

Doesn't open a real MQTT connection. The state-machine tests construct a
CerboMqtt with no-op callbacks and exercise rotation/sticky logic via the
public attributes paho would mutate during real reconnects.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus.config import MqttConfig
from cerberus.mqtt_client import (
    CerboMqtt,
    ROTATE_AFTER_ATTEMPTS,
    STICKY_CONNECT_SECONDS,
)


# ---------------------------------------------------------------------------
# config.load() — host vs hosts parsing
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def _stub_env(monkeypatch, password="pw", host_env=None):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", password)
    if host_env is None:
        monkeypatch.delenv("CERBERUS_MQTT_HOST", raising=False)
    else:
        monkeypatch.setenv("CERBERUS_MQTT_HOST", host_env)


def _minimal_body(mqtt_block: str) -> str:
    return textwrap.dedent(f"""
    mqtt:
      {mqtt_block}
      portal_id: abc
    packs:
      uc100: 1
    storage:
      sqlite_path: /tmp/x.db
    """)


def test_legacy_single_host(tmp_path, monkeypatch):
    _stub_env(monkeypatch)
    p = _write_cfg(tmp_path, _minimal_body("host: venus.local"))
    cfg = cfg_mod.load(p)
    assert cfg.mqtt.host == "venus.local"
    assert cfg.mqtt.hosts == ("venus.local",)


def test_hosts_list(tmp_path, monkeypatch):
    _stub_env(monkeypatch)
    body = textwrap.dedent("""
    mqtt:
      hosts:
        - venus.local
        - cerbo.tailnet.ts.net
        - mqtt99.victronenergy.com
      portal_id: abc
    packs:
      uc100: 1
    storage:
      sqlite_path: /tmp/x.db
    """)
    p = _write_cfg(tmp_path, body)
    cfg = cfg_mod.load(p)
    assert cfg.mqtt.host == "venus.local"
    assert cfg.mqtt.hosts == (
        "venus.local", "cerbo.tailnet.ts.net", "mqtt99.victronenergy.com",
    )


def test_env_overrides_with_csv(tmp_path, monkeypatch):
    _stub_env(monkeypatch, host_env="a.local, b.local ,c.local")
    p = _write_cfg(tmp_path, _minimal_body("host: should-be-ignored"))
    cfg = cfg_mod.load(p)
    assert cfg.mqtt.hosts == ("a.local", "b.local", "c.local")
    assert cfg.mqtt.host == "a.local"


def test_env_single_host_still_works(tmp_path, monkeypatch):
    _stub_env(monkeypatch, host_env="override.local")
    p = _write_cfg(tmp_path, _minimal_body("host: ignored.local"))
    cfg = cfg_mod.load(p)
    assert cfg.mqtt.hosts == ("override.local",)


def test_missing_host_raises(tmp_path, monkeypatch):
    _stub_env(monkeypatch)
    body = textwrap.dedent("""
    mqtt:
      portal_id: abc
    packs:
      uc100: 1
    storage:
      sqlite_path: /tmp/x.db
    """)
    p = _write_cfg(tmp_path, body)
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod.load(p)


# ---------------------------------------------------------------------------
# CerboMqtt rotation state machine
# ---------------------------------------------------------------------------

def _client(hosts: tuple[str, ...]) -> CerboMqtt:
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
    # construct only — never call .start()
    return CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None)


def test_single_host_no_rotation():
    c = _client(("only.local",))
    assert c.current_host == "only.local"
    for _ in range(10):
        c._note_failed_attempt()
    assert c.current_host == "only.local"


def test_rotates_after_threshold_attempts():
    c = _client(("a.local", "b.local", "c.local"))
    assert c.current_host == "a.local"
    for _ in range(ROTATE_AFTER_ATTEMPTS - 1):
        c._note_failed_attempt()
    assert c.current_host == "a.local"  # not yet
    c._note_failed_attempt()
    assert c.current_host == "b.local"
    # next round rotates further
    for _ in range(ROTATE_AFTER_ATTEMPTS):
        c._note_failed_attempt()
    assert c.current_host == "c.local"
    # wraps around
    for _ in range(ROTATE_AFTER_ATTEMPTS):
        c._note_failed_attempt()
    assert c.current_host == "a.local"


def test_rotate_host_wraps_modulo():
    c = _client(("a.local", "b.local"))
    c._rotate_host()
    assert c.current_host == "b.local"
    c._rotate_host()
    assert c.current_host == "a.local"


def test_attempts_reset_when_rotation_happens():
    c = _client(("a.local", "b.local"))
    for _ in range(ROTATE_AFTER_ATTEMPTS):
        c._note_failed_attempt()
    assert c.current_host == "b.local"
    # After rotation, the counter is back at 0 — proving subsequent rotation
    # also takes the full ROTATE_AFTER_ATTEMPTS hits.
    assert c._attempts_since_last_success == 0
