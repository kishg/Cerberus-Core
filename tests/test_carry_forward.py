"""Carry-forward of on-change topics while the feed is alive.

The GX publishes Batteries, temperatures and WiFi only on change. Once the
keepalive stopped forcing a full republish every 30 s (PR #10), a quiet night
left battery_samples with 2-4 min holes (10-14/15: 1086 of 1440 rows) although
solar and temperature rows kept arriving throughout. What to pin:
  * an unchanged topic keeps getting rows on the sample interval;
  * it stops once the topic has been silent past two full republishes;
  * nothing is carried when the caller sees the Cerbo offline (run() gates it).
"""

from __future__ import annotations

from cerberus import __main__ as main_mod
from cerberus.__main__ import Cerberus
from cerberus.db import Db
from cerberus.model import BankState, PackState


class _Storage:
    sample_downsample_seconds = 30


class _Mqtt:
    full_republish_seconds = 300


class _Cfg:
    storage = _Storage()
    mqtt = _Mqtt()


def _pack(pack_id, ts):
    return PackState(pack_id=pack_id, uc=f"uc{pack_id}", instance=pack_id,
                     soc=89.0, voltage=13.25, current=0.0, power=0.0,
                     temperature=21.0, timetogo=None, ts=ts)


def _collector(db, msg_ts):
    c = Cerberus.__new__(Cerberus)
    c.cfg = _Cfg()
    c.db = db
    c.bank = BankState(ts=msg_ts, packs={i: _pack(i, msg_ts) for i in (1, 2, 3)})
    c.last_sample_ts = msg_ts
    c.last_batteries_msg_ts = msg_ts
    c.temps = {24: {"temperature_c": 20.5, "raw_value": 0.31}}
    c.last_temp_sample_ts = {24: msg_ts}
    c.last_temp_msg_ts = {24: msg_ts}
    c.wifi = {"signal_strength": 45, "state": "ready"}
    c.last_wifi_sample_ts = msg_ts
    c.last_wifi_msg_ts = msg_ts
    return c


def _at(monkeypatch, t):
    monkeypatch.setattr(main_mod.time, "time", lambda: t)


def _count(db, table, where=""):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def test_unchanged_topics_keep_their_sample_interval(tmp_path, monkeypatch):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db, msg_ts=10_000)
    for t in range(10_005, 10_000 + 300, 5):       # 5 min of liveness ticks
        _at(monkeypatch, t)
        c._carry_forward_tick()
    # one write per 30 s step after the last real message: 10030 .. 10270
    assert _count(db, "battery_samples", "WHERE pack_id = 1") == 9
    assert _count(db, "temperature_samples") == 9
    assert _count(db, "cerbo_wifi_samples") == 9
    row = db.conn.execute(
        "SELECT ts, soc, voltage FROM battery_samples WHERE pack_id = 1 ORDER BY ts LIMIT 1"
    ).fetchone()
    assert row == (10_030, 89.0, 13.25)             # carried value, fresh ts


def test_carrying_stops_once_a_topic_is_silent_past_two_full_republishes(
        tmp_path, monkeypatch):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db, msg_ts=10_000)
    limit = 2 * 300 + main_mod.LIVENESS_CHECK_PERIOD
    _at(monkeypatch, 10_000 + limit + 30)
    c.last_sample_ts = c.last_temp_sample_ts[24] = c.last_wifi_sample_ts = 0
    c._carry_forward_tick()
    assert _count(db, "battery_samples") == 0
    assert _count(db, "temperature_samples") == 0
    assert _count(db, "cerbo_wifi_samples") == 0


def test_nothing_before_the_first_message(tmp_path, monkeypatch):
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db, msg_ts=10_000)
    c.bank = None
    c.temps = {}
    c.wifi = {}
    _at(monkeypatch, 10_060)
    c._carry_forward_tick()
    assert _count(db, "battery_samples") == 0
    assert _count(db, "temperature_samples") == 0
    assert _count(db, "cerbo_wifi_samples") == 0


def test_a_real_message_is_not_duplicated_by_a_carry(tmp_path, monkeypatch):
    """Carry only fills the interval; it must not write in the same step as a
    message-driven write."""
    db = Db(str(tmp_path / "t.db"))
    c = _collector(db, msg_ts=10_000)
    _at(monkeypatch, 10_020)                        # inside the 30 s step
    c._carry_forward_tick()
    assert _count(db, "battery_samples") == 0
