"""Cerbo temperature probes: paired fields, per-instance, throttled.

Instance 24 is why this exists. It reads 67 C at 56 A of charge current, so it
is clamped to a joint rather than measuring air, and which joint has never been
identified. Until 2026-09-21 it was stored nowhere locally — only on VRM at
15-minute resolution, which is far too coarse to correlate against a 30 s
current trace. That correlation is the thing that would identify it.

The two properties worth pinning: Temperature and RawValue arrive on separate
topics at different times and must end up on one row, and a probe that has only
published one of them must not have the other written as a zero.
"""

from __future__ import annotations

from cerberus.__main__ import Cerberus
from cerberus.db import Db


class _Storage:
    sample_downsample_seconds = 30


class _Cfg:
    storage = _Storage()


def _collector(db):
    # Built without __init__, which would stand up an MQTT client, publisher,
    # syncer and six detectors — none of which _on_temperature touches.
    c = Cerberus.__new__(Cerberus)
    c.cfg = _Cfg()
    c.db = db
    c.temps = {}
    c.last_temp_sample_ts = {}
    c.last_temp_msg_ts = {}
    return c


def test_temperature_and_rawvalue_land_on_one_row(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_temperature("N/p/temperature/24/RawValue", 3.018249)
    c._on_temperature("N/p/temperature/24/Temperature", 28.6825)
    row = db.conn.execute(
        "select instance, round(temperature_c,3), round(raw_value,3) "
        "from temperature_samples").fetchone()
    assert row == (24, 28.683, 3.018)
    db.close()


def test_rawvalue_alone_writes_nothing(tmp_path):
    """RawValue usually lands first. Writing on it alone would put a null
    temperature in the table for every probe's first row."""
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_temperature("N/p/temperature/24/RawValue", 3.0)
    assert db.conn.execute("select count(*) from temperature_samples").fetchone()[0] == 0
    assert c.temps[24] == {"raw_value": 3.0}, "but it must still be remembered"
    db.close()


def test_temperature_alone_writes_with_a_null_rawvalue(tmp_path):
    """A Bluetooth sensor publishes no RawValue at all; it must still log."""
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_temperature("N/p/temperature/31/Temperature", 21.0)
    row = db.conn.execute(
        "select temperature_c, raw_value from temperature_samples").fetchone()
    assert row == (21.0, None)
    db.close()


def test_multiple_instances_are_kept_apart(tmp_path):
    """A Bluetooth sensor paired to the Cerbo arrives as a second instance —
    it must not overwrite the hot-joint probe."""
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_temperature("N/p/temperature/24/Temperature", 66.5)
    c._on_temperature("N/p/temperature/31/Temperature", 21.0)
    rows = dict(db.conn.execute(
        "select instance, temperature_c from temperature_samples").fetchall())
    assert rows == {24: 66.5, 31: 21.0}
    db.close()


def test_writes_are_throttled_to_the_downsample_interval(tmp_path):
    """The tree updates several times a second; the table should not."""
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    for v in (20.0, 20.1, 20.2, 20.3):
        c._on_temperature("N/p/temperature/24/Temperature", v)
    n = db.conn.execute("select count(*) from temperature_samples").fetchone()[0]
    assert n == 1, "a burst of messages must produce one row, not four"
    db.close()


def test_malformed_topics_and_values_are_ignored(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    for topic, value in [
        ("N/p/temperature/24/Temperature", None),     # Victron publishes null
        ("N/p/temperature/x/Temperature", 20.0),      # non-numeric instance
        ("N/p/temperature/24/Temperature", "warm"),   # non-numeric value
        ("N/p/temperature/24/ProductName", "probe"),  # a field we do not store
    ]:
        c._on_temperature(topic, value)
    assert db.conn.execute("select count(*) from temperature_samples").fetchone()[0] == 0
    assert c.temps == {}
    db.close()
