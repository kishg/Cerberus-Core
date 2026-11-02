"""The Cerbo's own WiFi signal (platform/0/Network/Wifi/*).

Added 2026-10-08 after an evening of 2-11 min feed gaps while the Mac's
internet stayed up and VRM stayed complete: the Cerbo was falling off the WiFi
(signal 43). Two things to pin: Wifi/State must not be swallowed by the AC
branch (which claims every topic ending /State), and rows are throttled and
written only once a signal is known.
"""

from __future__ import annotations

import json

from cerberus.__main__ import Cerberus
from cerberus.db import Db
from cerberus.mqtt_client import CerboMqtt

P = "N/0123456789ab/platform/0/Network/Wifi"


class _Storage:
    sample_downsample_seconds = 30


class _Cfg:
    storage = _Storage()


def _collector(db):
    c = Cerberus.__new__(Cerberus)
    c.cfg = _Cfg()
    c.db = db
    c.wifi = {}
    c.last_wifi_sample_ts = 0
    return c


class _Msg:
    def __init__(self, topic, value):
        self.topic = topic
        self.payload = json.dumps({"value": value}).encode()


class _MqttCfg:
    primary_topic = "system/0/Batteries"


def _dispatcher(calls):
    m = CerboMqtt.__new__(CerboMqtt)
    m.on_dc = None
    m.cfg = _MqttCfg()
    m.on_wifi = lambda t, v: calls.append(("wifi", t, v))
    m.on_ac = lambda t, v: calls.append(("ac", t, v))
    m.on_solar = m.on_temperature = m.on_battery_health = m.on_alarm = None
    return m


def test_wifi_state_is_not_claimed_by_the_ac_branch():
    calls = []
    _dispatcher(calls)._on_message(None, None, _Msg(f"{P}/State", "ready"))
    assert calls == [("wifi", f"{P}/State", "ready")]


def test_signal_and_state_land_on_one_row(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_wifi(f"{P}/State", "ready")
    c._on_wifi(f"{P}/SignalStrength", 43)
    assert db.conn.execute(
        "select signal_strength, state from cerbo_wifi_samples").fetchall() == [(43, "ready")]


def test_state_alone_writes_nothing(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_wifi(f"{P}/State", "ready")
    c._on_wifi(f"{P}/Scan", 0)
    assert db.conn.execute("select count(*) from cerbo_wifi_samples").fetchone() == (0,)


def test_throttled_to_the_sample_interval(tmp_path, monkeypatch):
    import cerberus.__main__ as main_mod
    clock = [1_000_000]
    monkeypatch.setattr(main_mod.time, "time", lambda: clock[0])
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db)
    c._on_wifi(f"{P}/SignalStrength", 43)
    clock[0] += 10
    c._on_wifi(f"{P}/SignalStrength", 40)          # inside the interval: dropped
    clock[0] += 25
    c._on_wifi(f"{P}/SignalStrength", 38)          # 35 s later: written
    assert db.conn.execute(
        "select ts, signal_strength from cerbo_wifi_samples order by ts").fetchall() == [
        (1_000_000, 43), (1_000_035, 38)]
