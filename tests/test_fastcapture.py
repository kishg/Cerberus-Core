"""Full-rate Dc/0 capture around alarms and heavy loads (cerberus.fastcapture)."""

from __future__ import annotations

import pytest

from cerberus import fastcapture as fc
from cerberus.db import Db

INV_V = "N/x/vebus/276/Dc/0/Voltage"
PACK_A = "N/x/battery/1/Dc/0/Current"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def rig(tmp_path):
    db, clock = Db(str(tmp_path / "f.db")), Clock()
    return db, clock, fc.FastCapture(db, fc.Settings(), clock=clock)


def _rows(db):
    return db.conn.execute("SELECT ts, series, value FROM fast_samples ORDER BY ts").fetchall()


def _windows(db):
    return db.conn.execute("SELECT start_ts, end_ts, reason FROM fast_windows ORDER BY id").fetchall()


def _feed(clock, cap, seconds, topic=INV_V, value=12.5, step=1.0):
    for _ in range(int(seconds / step)):
        cap.on_dc(topic, value)
        clock.t += step


def test_series_names():
    assert fc.series_name(INV_V) == "vebus/276/Voltage"
    assert fc.series_name(PACK_A) == "battery/1/Current"
    assert fc.series_name("N/x/solarcharger/277/Dc/0/Voltage") == "solarcharger/277/Voltage"
    assert fc.series_name("N/x/vebus/276/Dc/0/Power") is None
    assert fc.series_name("N/x/system/0/Dc/Pv/Power") is None


def test_nothing_is_stored_outside_a_window(rig):
    db, clock, cap = rig
    _feed(clock, cap, 300)
    cap.tick()
    assert _rows(db) == [] and _windows(db) == []


def test_alarm_stores_the_minute_before_and_the_minute_after(rig):
    db, clock, cap = rig
    _feed(clock, cap, 300)
    alarm_at = clock.t
    cap.on_alarm("vebus/276/LowBattery")
    _feed(clock, cap, 90)
    clock.t += 5
    cap.tick()
    ts = [r[0] for r in _rows(db)]
    assert min(ts) >= alarm_at - 61 and min(ts) <= alarm_at - 58      # the ring
    assert max(ts) >= alarm_at + 58 and max(ts) <= alarm_at + 61      # stops after post
    (start, end, reason), = _windows(db)
    assert reason == "alarm:vebus/276/LowBattery"
    assert end == pytest.approx(alarm_at + 60)


def test_heavy_load_opens_and_closes_by_the_heater_rule(rig):
    db, clock, cap = rig
    for _ in range(60):
        cap.on_load(150.0)
        cap.on_dc(INV_V, 13.2)
        clock.t += 1
    assert _windows(db) == []
    on_at = clock.t
    cap.on_load(1100.0)                 # 950 W over a 150 W baseline
    for _ in range(120):
        cap.on_load(1100.0)
        cap.on_dc(INV_V, 12.45)
        clock.t += 1
    off_at = clock.t
    cap.on_load(160.0)                  # under baseline + 400 W
    _feed(clock, cap, 80, value=13.2)
    cap.tick()
    (start, end, reason), = _windows(db)
    assert reason == "load" and end == pytest.approx(off_at + 60)
    ts = [r[0] for r in _rows(db)]
    assert min(ts) <= on_at - 58 and max(ts) <= off_at + 61
    assert min(v for _, _, v in _rows(db)) == 12.45


def test_load_under_the_threshold_does_not_open_a_window(rig):
    db, clock, cap = rig
    for _ in range(60):
        cap.on_load(150.0)
        clock.t += 1
    cap.on_load(900.0)                  # 750 W over baseline: below on_delta
    cap.on_dc(INV_V, 13.0)
    cap.tick()
    assert _windows(db) == [] and _rows(db) == []


def test_inverter_off_ends_a_heavy_load(rig):
    db, clock, cap = rig
    cap.on_load(1500.0)
    assert cap._heavy
    cap.on_load(None)
    assert not cap._heavy


def test_an_alarm_during_a_load_joins_its_window(rig):
    db, clock, cap = rig
    cap.on_load(1500.0)
    _feed(clock, cap, 10)
    cap.on_alarm("vebus/276/LowBattery")
    cap.tick()
    (_, _, reason), = _windows(db)
    assert reason == "load,alarm:vebus/276/LowBattery"


def test_rate_limit_per_series(rig):
    db, clock, cap = rig
    cap.on_alarm("a/1/B")
    for _ in range(100):                # 100 messages in 1 s
        cap.on_dc(INV_V, 12.5)
        cap.on_dc(PACK_A, -30.0)
        clock.t += 0.01
    clock.t += 3
    cap.tick()
    by = {}
    for _, s, _ in _rows(db):
        by[s] = by.get(s, 0) + 1
    assert by == {"vebus/276/Voltage": 4, "battery/1/Current": 4}   # one per 0.25 s


def test_null_and_text_values_are_ignored(rig):
    db, clock, cap = rig
    cap.on_alarm("a/1/B")
    cap.on_dc(INV_V, None)
    cap.on_dc(INV_V, "x")
    clock.t += 3
    cap.tick()
    assert _rows(db) == []


def test_pending_rows_flush_every_few_seconds_while_active(rig):
    db, clock, cap = rig
    cap.on_alarm("a/1/B")
    cap.on_dc(INV_V, 12.5)
    cap.tick()
    clock.t += 3
    cap.tick()
    assert len(_rows(db)) == 1


def test_old_rows_are_pruned(rig):
    db, clock, cap = rig
    db.insert_fast_samples([(clock.t - 100 * 86400, "vebus/276/Voltage", 12.0),
                            (clock.t - 86400, "vebus/276/Voltage", 12.1)])
    db.open_fast_window(clock.t - 100 * 86400, "load")
    clock.t += 1
    cap._last_prune = 0
    clock.t = max(clock.t, 90000.0)
    cap.tick()
    assert [r[2] for r in _rows(db)] == [12.1]
    assert _windows(db) == []


def test_disabled_does_nothing(tmp_path):
    db, clock = Db(str(tmp_path / "f.db")), Clock()
    cap = fc.FastCapture(db, fc.Settings(enabled=False), clock=clock)
    cap.on_alarm("a/1/B")
    cap.on_load(5000.0)
    cap.on_dc(INV_V, 12.5)
    clock.t += 5
    cap.tick()
    assert _rows(db) == [] and _windows(db) == []


def test_mqtt_client_hands_every_dc_message_to_on_dc():
    import json
    from types import SimpleNamespace
    from cerberus.mqtt_client import CerboMqtt
    seen, ac = [], []
    m = CerboMqtt.__new__(CerboMqtt)
    m.cfg = SimpleNamespace(primary_topic="system/0/Batteries")
    for name in ("on_batteries", "on_heartbeat", "on_location", "on_wifi", "on_temperature",
                 "on_battery_health", "on_alarm", "on_solar"):
        setattr(m, name, None)
    m.on_ac = lambda t, v: ac.append(t)
    m.on_dc = lambda t, v: seen.append((t, v))
    for topic in (INV_V, PACK_A, "N/x/vebus/276/State"):
        m._on_message(None, None, SimpleNamespace(
            topic=topic, payload=json.dumps({"value": 12.5}).encode()))
    assert seen == [(INV_V, 12.5), (PACK_A, 12.5)]
    assert ac == [INV_V, "N/x/vebus/276/State"]        # folding into state is unchanged
