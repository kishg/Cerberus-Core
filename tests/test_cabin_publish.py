"""Cabin sensor -> HA: newest row per sensor, gated so it can never go stale.

The cabin H5179 is written by the BLE sidecar and by the Govee cloud poller,
neither of which speaks to HA, so the collector reads the newest row back out
of SQLite and publishes it.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from cerberus.db import CabinSample, Db
from cerberus.publisher import Publisher

from tests.test_publisher_availability import FakeMqtt


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as d:
        yield Db(str(Path(d) / "test.db"))


def _pub(sensors=("cabin",)):
    m = FakeMqtt()
    return m, Publisher(mqtt=m, namespace="cerberus", discovery_prefix="homeassistant",
                        pack_ids=(1, 2, 3), cabin_sensors=sensors)


def test_latest_cabin_takes_newest_row_across_sources(db):
    now = int(time.time())
    db.insert_cabin_sample(now - 300, "cabin", 61.0, 55.0, None, source="govee_cloud")
    db.insert_cabin_sample(now - 30, "cabin", 59.2, 60.1, 90, rssi=-70)
    out = db.latest_cabin_per_sensor(max_age_seconds=900)
    assert out["cabin"].air_temp_f == 59.2
    assert out["cabin"].source == "ble"


def test_latest_cabin_freshness_filter(db):
    db.insert_cabin_sample(int(time.time()) - 3600, "cabin", 59.0, 60.0, None)
    assert db.latest_cabin_per_sensor(max_age_seconds=900) == {}


def test_cabin_discovery_is_fahrenheit_temperature_with_gate():
    m, p = _pub()
    p.publish_discovery()
    cfg = json.loads(m.topics["homeassistant/sensor/cerberus/cabin_temperature/config"])
    assert cfg["unit_of_measurement"] == "°F"
    assert cfg["device_class"] == "temperature"
    assert cfg["state_class"] == "measurement"     # long-term statistics for graphs
    assert {"topic": "cerberus/cabin/cabin/temperature/avail",
            "payload_available": "online",
            "payload_not_available": "offline"} in cfg["availability"]
    assert "homeassistant/sensor/cerberus/cabin_humidity/config" in m.topics


def test_no_cabin_entities_when_no_sensors_configured():
    m, p = _pub(sensors=())
    p.publish_discovery()
    assert not any("cabin" in t for t in m.topics)


def test_publish_cabin_state_then_goes_unavailable_when_stale():
    m, p = _pub()
    p.publish_cabin_state({"cabin": CabinSample(ts=1, sensor="cabin", air_temp_f=59.18,
                                                humidity_pct=60.0, source="ble")})
    assert m.topics["cerberus/cabin/cabin/temperature"] == "59.2"
    assert m.topics["cerberus/cabin/cabin/temperature/avail"] == "online"
    p.publish_cabin_state({})
    assert m.topics["cerberus/cabin/cabin/temperature/avail"] == "offline"
    assert m.topics["cerberus/cabin/cabin/humidity/avail"] == "offline"


# --- engine: same rule, the last drive's RPM must not linger (2026-11-01) ------

def test_engine_stale_marks_readings_unavailable_and_not_running():
    m, p = _pub()
    p.publish_engine_stale()
    for name in ("rpm", "speed_mph", "coolant_c", "load_pct"):
        assert m.topics[f"cerberus/engine/{name}/avail"] == "offline"
    assert m.topics["cerberus/engine/running"] == "off"


def test_collector_clears_engine_once_when_rows_stop(monkeypatch):
    from cerberus.__main__ import Cerberus
    calls = []

    class Pub:
        def publish_engine_stale(self):
            calls.append("stale")

        def publish_engine_state(self, s):
            calls.append("live")

    rows = iter([None, None, object(), None])

    class DbStub:
        def latest_engine_sample(self, max_age_seconds):
            return next(rows)

    c = Cerberus.__new__(Cerberus)
    c.publisher, c.db, c.last_obd_tick_ts, c._engine_live = Pub(), DbStub(), 0, True
    for _ in range(4):
        c.last_obd_tick_ts = 0
        c._obd_tick()
    assert calls == ["stale", "live", "stale"]
