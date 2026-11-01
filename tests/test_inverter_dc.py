"""Inverter DC terminal voltage: MQTT routing and ac_samples storage."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cerberus.config import MqttConfig
from cerberus.db import Db
from cerberus.mqtt_client import CerboMqtt


def _client(seen: list) -> CerboMqtt:
    cfg = MqttConfig(
        host="venus.local", hosts=("venus.local",), port=1883, portal_id="abc",
        username="abc", password="pw", keepalive_publish_seconds=30,
        primary_topic="system/0/Batteries", publish_namespace="cerberus",
        ha_discovery_prefix="homeassistant",
    )
    return CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None,
                     on_ac=lambda t, v: seen.append((t, v)))


def _msg(topic: str, value) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=json.dumps({"value": value}).encode())


def test_vebus_dc_routes_to_ac_handler():
    seen: list = []
    c = _client(seen)
    c._on_message(None, None, _msg("N/abc/vebus/276/Dc/0/Voltage", 12.41))
    assert seen == [("N/abc/vebus/276/Dc/0/Voltage", 12.41)]


def test_battery_dc_does_not_route_to_ac_handler():
    seen: list = []
    c = _client(seen)
    c._on_message(None, None, _msg("N/abc/battery/512/Dc/0/Voltage", 13.3))
    assert seen == []


def test_columns_stored():
    with tempfile.TemporaryDirectory() as d:
        db = Db(str(Path(d) / "t.db"))
        db.insert_ac_sample(ts=1, consumption_w=1500, input_w=None, output_w=1500,
                            shore_connected=False, inverter_state=9,
                            inverter_dc_voltage=12.41, inverter_dc_current=-146.2)
        row = db.conn.execute(
            "SELECT inverter_dc_voltage, inverter_dc_current FROM ac_samples").fetchone()
        assert tuple(row) == (12.41, -146.2)
        db.close()


def test_migration_adds_columns_to_old_db():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "old.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE ac_samples (ts INTEGER PRIMARY KEY, consumption_w REAL, "
                  "input_w REAL, output_w REAL, shore_connected INTEGER, "
                  "inverter_state INTEGER, source TEXT NOT NULL DEFAULT 'mqtt')")
        c.execute("INSERT INTO ac_samples (ts, inverter_state) VALUES (1, 0)")
        c.commit()
        c.close()
        db = Db(path)
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(ac_samples)")}
        assert {"inverter_dc_voltage", "inverter_dc_current"} <= cols
        assert db.conn.execute("SELECT inverter_dc_voltage FROM ac_samples").fetchone()[0] is None
        db.close()
