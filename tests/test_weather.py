"""Outdoor conditions capture: unit conversion, failure isolation, storage.

This series exists to close a measurement gap rather than to fix a bug. On
2026-09-19 the question "does load fall as fast as solar does through the
autumn?" could not be answered, because the only temperature in the system is
the Lithionics pack reading — and daily DC load correlated with it at
r = +0.016 across 34 days. The packs are in a cabinet with thermal mass and
self-heat under charge; they are not an ambient proxy.

The two properties that actually matter here are both about *not* breaking
things that already work:

* A weather failure must never reach the liveness loop. met.no is a
  third-party HTTP dependency on a van that is frequently on a marginal
  Starlink link, and the loop it runs inside is the one that watches the Cerbo.
* The unit must be unambiguous at the schema level. The project convention is
  ambient in F and equipment in C, and the way that gets violated is a bare
  `temperature` column whose unit lives only in someone's memory.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error

import pytest

from cerberus import weather as weather_mod
from cerberus.db import Db


PAYLOAD = {
    "properties": {
        "timeseries": [
            {"data": {"instant": {"details": {
                "air_temperature": 20.0,          # met.no publishes Celsius
                "cloud_area_fraction": 80.5,
                "relative_humidity": 58.0,
                "wind_speed": 3.3,                # m/s
            }}}},
            {"data": {"instant": {"details": {"air_temperature": 99.0}}}},
        ]
    }
}


class _Resp:
    def __init__(self, payload): self._p = payload
    def read(self): return json.dumps(self._p).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch(monkeypatch, fn):
    monkeypatch.setattr(weather_mod.urllib.request, "urlopen", fn)


def test_celsius_is_converted_to_fahrenheit(monkeypatch):
    """20 C is 68 F. The column is air_temp_f and must actually hold F."""
    _patch(monkeypatch, lambda req, timeout=None: _Resp(PAYLOAD))
    s = weather_mod.fetch(40.87, -74.38, "test/1")
    assert s.air_temp_f == 68.0
    assert s.cloud_pct == 80.5
    assert s.humidity_pct == 58.0
    assert s.wind_mph == 7.4          # 3.3 m/s


def test_uses_the_nearest_timestep_not_a_later_forecast(monkeypatch):
    """timeseries[0] is now; [1] is an hour out. Pairing a forecast with a
    30 s electrical sample would silently decorrelate the whole point."""
    _patch(monkeypatch, lambda req, timeout=None: _Resp(PAYLOAD))
    assert weather_mod.fetch(40.87, -74.38, "test/1").air_temp_f == 68.0


def test_identifying_user_agent_is_sent(monkeypatch):
    """met.no returns 403 without one — verified live 2026-09-19, where a
    literal example.com contact was also rejected."""
    seen = {}
    def fake(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _Resp(PAYLOAD)
    _patch(monkeypatch, fake)
    weather_mod.fetch(40.87, -74.38, "Cerberus/0.1 (contact)")
    assert seen["ua"] == "Cerberus/0.1 (contact)"


@pytest.mark.parametrize("boom", [
    urllib.error.URLError("no route to host"),
    urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
    TimeoutError("timed out"),
    OSError("connection reset"),
])
def test_network_failure_returns_none_never_raises(monkeypatch, boom):
    """The van is often on a marginal link. A weather gap is acceptable; an
    exception escaping into the Cerbo liveness loop is not."""
    def fake(req, timeout=None): raise boom
    _patch(monkeypatch, fake)
    assert weather_mod.fetch(40.87, -74.38, "test/1") is None


@pytest.mark.parametrize("payload", [
    {}, {"properties": {}}, {"properties": {"timeseries": []}},
    {"properties": {"timeseries": [{"data": {}}]}},
])
def test_malformed_payload_returns_none(monkeypatch, payload):
    """A third-party schema change must degrade to a gap, not a crash."""
    _patch(monkeypatch, lambda req, timeout=None: _Resp(payload))
    assert weather_mod.fetch(40.87, -74.38, "test/1") is None


def test_missing_fields_are_none_not_zero(monkeypatch):
    """'Absent is not zero' — the same rule the solar detector relies on. A
    missing cloud reading must not land as 0% and read as a clear sky."""
    _patch(monkeypatch, lambda req, timeout=None: _Resp(
        {"properties": {"timeseries": [{"data": {"instant": {"details": {
            "air_temperature": 10.0}}}}]}}))
    s = weather_mod.fetch(40.87, -74.38, "test/1")
    assert s.air_temp_f == 50.0
    assert s.cloud_pct is None and s.humidity_pct is None and s.wind_mph is None


def test_row_round_trips_and_column_says_fahrenheit(tmp_path):
    db = Db(str(tmp_path / "w.db"))
    cols = {c[1] for c in db.conn.execute("PRAGMA table_info(weather_samples)")}
    assert "air_temp_f" in cols, "unit must be in the column name"
    assert "temperature" not in cols, "bare 'temperature' invites a C/F mixup"
    assert db.insert_weather_sample(1789856831, 67.6, 80.5, 58.0, 7.4)
    row = db.conn.execute(
        "select ts, air_temp_f, cloud_pct, humidity_pct, wind_mph, source "
        "from weather_samples").fetchone()
    assert row == (1789856831, 67.6, 80.5, 58.0, 7.4, "metno")
    db.close()


def test_insert_is_idempotent_on_ts(tmp_path):
    """Same PRIMARY KEY(ts) discipline as solar_samples/ac_samples, so a
    restart inside one interval cannot double-count."""
    db = Db(str(tmp_path / "w.db"))
    db.insert_weather_sample(100, 60.0, 10.0, 50.0, 1.0)
    db.insert_weather_sample(100, 70.0, 20.0, 60.0, 2.0)
    assert db.conn.execute("select count(*) from weather_samples").fetchone()[0] == 1
    assert db.conn.execute("select air_temp_f from weather_samples").fetchone()[0] == 70.0
    db.close()
