"""Cabin readings via the Govee cloud API (cerberus.govee_cloud).

The cloud is the fallback for when this machine is out of BLE range of the van.
What matters is the same as for weather — a third-party failure must never
reach the liveness loop — plus two things specific to this source: an offline
sensor's stale value must not be stored as a reading, and a cloud row must
never overwrite a real BLE sample.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import urllib.error

import pytest

from cerberus import govee_cloud as gc
from cerberus.db import Db

# Shape captured live on 2026-10-04 (64.22 matched the BLE decode's 64.2 F).
LIVE = {
    "requestId": "cerberus-probe", "msg": "success", "code": 200,
    "payload": {
        "sku": "H5179", "device": "00:00:00:00:00:00:00:01",
        "capabilities": [
            {"type": "devices.capabilities.online", "instance": "online",
             "state": {"value": True}},
            {"type": "devices.capabilities.property", "instance": "sensorTemperature",
             "state": {"value": 64.22}},
            {"type": "devices.capabilities.property", "instance": "sensorHumidity",
             "state": {"value": 59.7}},
        ],
    },
}


def _offline():
    p = json.loads(json.dumps(LIVE))
    p["payload"]["capabilities"][0]["state"]["value"] = False
    return p


class _Resp:
    def __init__(self, payload): self._p = payload
    def read(self): return json.dumps(self._p).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch(monkeypatch, fn):
    monkeypatch.setattr(gc.urllib.request, "urlopen", fn)


def test_live_payload_parses_as_fahrenheit():
    r = gc.parse_state(LIVE)
    assert r == gc.CloudReading(air_temp_f=64.22, humidity_pct=59.7)


def test_offline_sensor_is_not_a_reading():
    assert gc.parse_state(_offline()) is None


def test_api_error_is_not_a_reading():
    assert gc.parse_state({"code": 401, "msg": "invalid key"}) is None


def test_request_carries_key_device_and_post(monkeypatch):
    seen = {}

    def fake(req, timeout=None):
        seen["key"] = req.get_header("Govee-api-key")
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data)
        return _Resp(LIVE)

    _patch(monkeypatch, fake)
    assert gc.fetch("k-123", "H5179", "AA:BB") is not None
    assert seen["key"] == "k-123"
    assert seen["method"] == "POST"
    assert seen["body"]["payload"] == {"sku": "H5179", "device": "AA:BB"}
    assert seen["body"]["requestId"]


@pytest.mark.parametrize("boom", [
    urllib.error.URLError("no route to host"),
    urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
    TimeoutError(),
    OSError("network down"),
])
def test_network_failures_return_none(monkeypatch, boom):
    def fake(req, timeout=None):
        raise boom
    _patch(monkeypatch, fake)
    assert gc.fetch("k", "H5179", "AA:BB") is None


def test_garbage_shape_returns_none(monkeypatch):
    _patch(monkeypatch, lambda req, timeout=None: _Resp({"code": 200, "payload": "nope"}))
    assert gc.fetch("k", "H5179", "AA:BB") is None


def _db() -> Db:
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_cloud_row_never_overwrites_a_ble_row():
    db = _db()
    db.insert_cabin_sample(100, "cabin", 64.2, 59.7, 93, rssi=-90)
    db.insert_cabin_sample(100, "cabin", 70.0, 50.0, None, source="govee_cloud")
    assert db.conn.execute(
        "SELECT air_temp_f, battery_pct, source FROM cabin_samples"
    ).fetchall() == [(64.2, 93, "ble")]


def test_ble_row_replaces_a_cloud_row_at_the_same_second():
    db = _db()
    db.insert_cabin_sample(100, "cabin", 70.0, 50.0, None, source="govee_cloud")
    db.insert_cabin_sample(100, "cabin", 64.2, 59.7, 93)
    assert db.conn.execute(
        "SELECT air_temp_f, source FROM cabin_samples"
    ).fetchall() == [(64.2, "ble")]


def test_migration_tags_existing_cabin_rows_as_ble():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE cabin_samples (ts INTEGER NOT NULL, sensor TEXT NOT NULL, "
        "air_temp_f REAL, humidity_pct REAL, battery_pct INTEGER, rssi INTEGER, "
        "PRIMARY KEY (ts, sensor));"
        "INSERT INTO cabin_samples VALUES (1, 'cabin', 64.0, 60.0, 93, -90);"
    )
    conn.commit()
    conn.close()
    db = Db(path)
    assert db.conn.execute("SELECT source FROM cabin_samples").fetchone() == ("ble",)
