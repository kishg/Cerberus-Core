"""Step 5.6a: Home Assistant is optional, and what it shows is right for any
van: device metadata, and a monitor-measured bank named as one battery."""

from __future__ import annotations

import importlib.metadata
import json
import shutil
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus import settings
from cerberus.firstrun import _config_text
from cerberus.ha_mqtt import NoBroker
from cerberus.publisher import DEVICE, Publisher

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


class Recorder:
    def __init__(self):
        self.sent = {}

    def publish(self, topic, payload, retain=False, qos=0):
        self.sent[topic] = payload


def _discovery(**kw):
    rec = Recorder()
    Publisher(mqtt=rec, namespace="cerberus", discovery_prefix="homeassistant", **kw).publish_discovery()
    return {json.loads(p)["unique_id"]: json.loads(p)["name"]
            for t, p in rec.sent.items() if t.endswith("/config")}


def test_off_for_a_new_install_on_for_this_van(tmp_path):
    import yaml
    fresh = yaml.safe_load(_config_text(["10.0.0.5"], "abc", "/tmp/c.db"))
    assert cfg_mod.from_raw({**fresh, "packs": {"uc1": 1}}, password="x").ha_mqtt.enabled is False
    assert cfg_mod.from_raw(cfg_mod.read_raw(REPO_CONFIG), password="x").ha_mqtt.enabled is True


def test_enabled_must_be_a_yes_or_no(tmp_path):
    raw = cfg_mod.read_raw(REPO_CONFIG)
    raw["ha_mqtt"]["enabled"] = "sometimes"
    with pytest.raises(cfg_mod.ConfigError, match="ha_mqtt.enabled"):
        cfg_mod.from_raw(raw, password="x")


def test_no_broker_publishes_nothing():
    b = NoBroker()
    b.start()
    b.publish("cerberus/x", "1", retain=True)
    b.stop()
    assert b.connected is False


def test_device_metadata():
    assert DEVICE["identifiers"] == ["cerberus"] and DEVICE["name"] == "Cerberus"   # HA keys on these
    assert DEVICE["sw_version"] == importlib.metadata.version("cerberus")
    assert "DIY" not in DEVICE["manufacturer"]


def test_packs_keep_their_names():
    names = _discovery(pack_ids=(1, 2, 3))
    assert names["cerberus_pack2_soc"] == "Pack 2 SoC"
    assert "cerberus_current_share_pack3" in names and "cerberus_soc_spread" in names


def test_a_monitor_is_named_as_the_battery():
    names = _discovery(pack_ids=(1,), monitor=True)
    assert names["cerberus_pack1_soc"] == "Battery SoC"            # same unique_id and topic
    assert names["cerberus_pack1_voltage"] == "Battery Voltage"
    assert "cerberus_current_share_pack1" not in names and "cerberus_soc_spread" not in names


@pytest.mark.parametrize("given, want", [("off", False), ("false", False), ("on", True), (True, True)])
def test_turning_it_on_and_off_in_settings(tmp_path, given, want):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    rows = {r["key"]: r for r in settings.apply(cfg, {"ha_mqtt.enabled": given})}
    assert rows["ha_mqtt.enabled"]["value"] is want


def test_a_bad_switch_value_is_refused(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    with pytest.raises(cfg_mod.ConfigError, match="on or off"):
        settings.apply(cfg, {"ha_mqtt.enabled": "maybe"})
