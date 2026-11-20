"""Idle shut-off (cerberus.inverter_idle): when the inverter goes to
charger-only, what brings it back, and what it must never do.

This is the first thing Cerberus writes to the Victron system, so the cases
that must NOT write are covered as carefully as the ones that must.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone

import pytest

from cerberus import config as cfg_mod
from cerberus import inverter_idle as m
from cerberus.mqtt_client import CerboMqtt

UTC = timezone.utc
ON, CHG = m.MODE_ON, m.MODE_CHARGER_ONLY


def ts(hh: int, mm: int = 0, day: int = 18) -> int:
    return int(datetime(2026, 11, day, hh, mm, tzinfo=UTC).timestamp())


S = m.Settings(enabled=True, threshold_w=150, minutes=15, hold_minutes=60,
               morning_on="07:00", tz=UTC)


def r(now, load=100.0, mode=ON, state=m.STATE_INVERTING, shore=False,
      connected=True, request=False):
    return m.Readings(now=now, load_w=load, mode=mode, inverter_state=state,
                      shore_connected=shore, connected=connected, turn_on_requested=request)


def run(settings, st, readings):
    """Feed a sequence; return (state, writes, event types)."""
    writes, events = [], []
    for rd in readings:
        d = m.decide(settings, st, rd)
        st = d.state
        if d.write_mode is not None:
            writes.append(d.write_mode)
        events += [e.type for e in d.events]
    return st, writes, events


def idle_minutes(start, minutes, **kw):
    return [r(start + 60 * i, **kw) for i in range(minutes + 1)]


# -- shut-off --------------------------------------------------------------

def test_idle_for_the_full_time_writes_charger_only_once():
    st, writes, _ = run(S, m.State(), idle_minutes(ts(23), 15))
    assert writes == [CHG]
    assert st.pending_mode == CHG and not st.off_by_idle


def test_one_reading_at_or_above_threshold_restarts_the_clock():
    seq = idle_minutes(ts(23), 10) + [r(ts(23, 11), load=150)] + idle_minutes(ts(23, 12), 10)
    _, writes, _ = run(S, m.State(), seq)
    assert writes == []


def test_confirmed_by_read_back_sets_off_by_idle_and_reports():
    st, _, _ = run(S, m.State(), idle_minutes(ts(23), 15))
    st, writes, events = run(S, st, [r(ts(23, 16), load=0, mode=CHG, state=0)])
    assert st.off_by_idle and st.pending_mode is None and st.off_ts == ts(23, 16)
    assert events == ["inverter_idle_off"] and writes == []


def test_unconfirmed_write_is_reported_and_not_retried_at_once():
    st, _, _ = run(S, m.State(), idle_minutes(ts(23), 15))
    st, writes, events = run(S, st, [r(ts(23, 15) + 30), r(ts(23, 16) + 5)])
    assert events == ["inverter_write_failed"] and writes == []
    # The retry floor holds the next attempt off for RETRY_SECONDS.
    _, writes, _ = run(S, st, idle_minutes(ts(23, 17), 2))
    assert writes == []


@pytest.mark.parametrize("kw", [
    {"shore": True},                       # shore: nothing to save
    {"state": 3},                          # charging, not inverting
    {"mode": m.MODE_OFF},                  # off by hand
    {"mode": m.MODE_INVERTER_ONLY},
    {"connected": False},                  # writes need the local link
    {"load": None},                        # no reading is not idle
])
def test_never_shuts_off_when(kw):
    _, writes, _ = run(S, m.State(), idle_minutes(ts(23), 20, **kw))
    assert writes == []


def test_disabled_never_shuts_off():
    _, writes, _ = run(m.Settings(tz=UTC), m.State(), idle_minutes(ts(23), 30))
    assert m.DEFAULT_SETTINGS.enabled is False
    assert writes == []


def test_quiet_window_across_midnight():
    q = m.Settings(**{**S.__dict__, "quiet_start": "18:00", "quiet_end": "00:30"})
    assert m.in_quiet(q, ts(23)) and m.in_quiet(q, ts(0, 15, day=19))
    assert not m.in_quiet(q, ts(0, 30, day=19)) and not m.in_quiet(q, ts(17, 59))
    _, writes, _ = run(q, m.State(), idle_minutes(ts(23), 20))
    assert writes == []
    _, writes, _ = run(q, m.State(), idle_minutes(ts(0, 30, day=19), 15))
    assert writes == [CHG]


# -- back on ---------------------------------------------------------------

def off_state(off_ts=ts(23, 16)):
    return m.State(off_by_idle=True, off_ts=off_ts, last_mode=CHG, last_write_ts=off_ts - 60)


def test_morning_turns_it_back_on_once_the_time_has_passed():
    st, writes, _ = run(S, off_state(), [r(ts(6, 59, day=19), load=0, mode=CHG, state=0)])
    assert writes == []
    st, writes, _ = run(S, st, [r(ts(7, 0, day=19), load=0, mode=CHG, state=0)])
    assert writes == [ON]
    st, writes, events = run(S, st, [r(ts(7, 0, day=19) + 10)])
    assert events == ["inverter_on"] and not st.off_by_idle
    assert st.hold_until == ts(7, 0, day=19) + 10 + 3600


def test_shut_off_before_dawn_waits_for_that_mornings_time_not_the_next():
    st, writes, _ = run(S, off_state(ts(2, 0, day=19)), [r(ts(7, 1, day=19), load=0, mode=CHG, state=0)])
    assert writes == [ON]


def test_no_morning_time_means_it_stays_off():
    s = m.Settings(**{**S.__dict__, "morning_on": None})
    _, writes, _ = run(s, off_state(), [r(ts(12, 0, day=19), load=0, mode=CHG, state=0)])
    assert writes == []


def test_button_turns_it_on_even_when_set_by_hand_and_feature_disabled():
    st = m.State(last_mode=CHG)
    _, writes, _ = run(m.Settings(tz=UTC), st, [r(ts(12), load=0, mode=CHG, state=0, request=True)])
    assert writes == [ON]


def test_button_needs_the_local_link():
    _, writes, _ = run(S, off_state(), [r(ts(1, day=19), load=0, mode=CHG, state=0,
                                          connected=False, request=True)])
    assert writes == []


def test_hold_after_turning_on_stops_an_immediate_shut_off():
    st = m.State(last_mode=ON, hold_until=ts(8))
    _, writes, _ = run(S, st, idle_minutes(ts(7, 10), 40))
    assert writes == []
    _, writes, _ = run(S, st, idle_minutes(ts(8), 15))
    assert writes == [CHG]


def test_a_hand_change_clears_the_shut_off_and_is_never_fought():
    st, writes, events = run(S, off_state(), [r(ts(1, day=19), load=40, mode=ON)])
    assert events == ["inverter_idle_cleared"] and writes == []
    assert not st.off_by_idle and st.hold_until == ts(1, day=19) + 3600
    # Owner switched it fully off: no morning turn-on, no writes.
    st, writes, _ = run(S, off_state(), [r(ts(7, 30, day=19), load=None, mode=m.MODE_OFF, state=0)])
    assert writes == [] and not st.off_by_idle


def test_state_round_trips_through_kv_and_tolerates_junk():
    st = off_state()
    assert m.State.from_json(st.to_json()) == st
    assert m.State.from_json("not json") == m.State()
    assert m.State.from_json('{"off_by_idle": true, "unknown": 1}').off_by_idle


# -- config & settings -----------------------------------------------------

def test_config_defaults_and_validation():
    parse = cfg_mod._parse_inverter_idle
    d = parse({})
    assert (d.enabled, d.threshold_w, d.minutes, d.morning_on, d.quiet_start) == (
        False, 150.0, 15.0, "07:00", None)
    assert parse({"inverter_idle": {"morning_on": None}}).morning_on is None
    assert parse({"inverter_idle": {"morning_on": "6:30"}}).morning_on == "06:30"
    for bad in ({"morning_on": "25:00"}, {"threshold_w": 0}, {"minutes": 0},
                {"quiet_start": "18:00"}, {"enabled": "maybe"}):
        with pytest.raises(cfg_mod.ConfigError):
            parse({"inverter_idle": bad})


def test_settings_screen_lists_threshold_and_time():
    from cerberus import settings as settings_mod
    keys = {s.key for s in settings_mod.SETTINGS}
    assert {"inverter_idle.enabled", "inverter_idle.threshold_w",
            "inverter_idle.minutes", "inverter_idle.morning_on"} <= keys


def test_request_file_round_trip(tmp_path):
    cfg = tmp_path / "config.yaml"
    assert m.read_request(cfg) is None
    m.write_request(cfg, 1234, "100.1.2.3")
    assert m.read_request(cfg) == 1234
    assert json.loads(m.request_path(cfg).read_text())["via"] == "100.1.2.3"


# -- the write itself ------------------------------------------------------

def _client(own_topics=True, connected=True):
    c = object.__new__(CerboMqtt)
    c.cfg = types.SimpleNamespace(portal_id="0123456789ab")
    c.own_topics = own_topics
    c._connected = connected
    c.sent = []
    c.client = types.SimpleNamespace(publish=lambda t, payload, qos: c.sent.append((t, payload, qos)))
    return c


def test_write_only_mode_1_or_3_on_the_local_link():
    c = _client()
    assert c.write_vebus_mode(276, CHG) is True
    assert c.sent == [("W/0123456789ab/vebus/276/Mode", b'{"value": 1}', 1)]
    for bad in (m.MODE_OFF, m.MODE_INVERTER_ONLY, 0):
        with pytest.raises(ValueError):
            c.write_vebus_mode(276, bad)
    cloud = _client(own_topics=False)
    assert cloud.write_vebus_mode(276, ON) is False and cloud.sent == []
    down = _client(connected=False)
    assert down.write_vebus_mode(276, ON) is False and down.sent == []


# -- the collector's tick --------------------------------------------------

def _app(tmp_path, mode=CHG, state=None):
    from cerberus.__main__ import Cerberus
    from cerberus.ac import AcState
    app = object.__new__(Cerberus)
    app.ac = AcState()
    app.ac.update("N/x/vebus/276/Mode", mode)
    app.ac.update("N/x/vebus/276/State", 0 if mode == CHG else m.STATE_INVERTING)
    kv, events = {}, []
    app.db = types.SimpleNamespace(set_kv=kv.__setitem__, insert_event=events.append)
    app.publisher = types.SimpleNamespace(publish_event=lambda ev: None)
    app.mqtt = _client()
    app.cfg = types.SimpleNamespace(inverter_idle=m.Settings(tz=UTC))
    app.van_tz = lambda: UTC
    app.config_path = tmp_path / "config.yaml"
    app.idle_state = state or m.State(last_mode=mode)
    app.idle_request_handled = 0
    return app, kv, events


def test_tick_acts_on_a_fresh_button_press_once(tmp_path):
    import time as time_mod
    app, kv, _ = _app(tmp_path)
    m.write_request(app.config_path, int(time_mod.time()), "test")
    app._inverter_idle_tick(online=True)
    assert app.mqtt.sent == [("W/0123456789ab/vebus/276/Mode", b'{"value": 3}', 1)]
    assert kv["inverter_idle.request_handled"] == str(app.idle_request_handled)
    assert json.loads(kv[m.KV_STATE])["pending_mode"] == ON
    app._inverter_idle_tick(online=True)          # pending, and the press is used up
    assert len(app.mqtt.sent) == 1


def test_tick_ignores_a_stale_press_and_an_offline_cerbo(tmp_path):
    import time as time_mod
    app, _, _ = _app(tmp_path)
    m.write_request(app.config_path, int(time_mod.time()) - m.REQUEST_FRESH_SECONDS - 5, "test")
    app._inverter_idle_tick(online=True)
    assert app.mqtt.sent == []
    m.write_request(app.config_path, int(time_mod.time()), "test")
    app._inverter_idle_tick(online=False)
    assert app.mqtt.sent == [] and app.idle_request_handled == 0   # kept for when it's back
