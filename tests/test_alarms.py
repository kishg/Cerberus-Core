"""Tests for the Victron alarm-mirroring layer (B3)."""

from __future__ import annotations

from cerberus.alarms import AlarmState, parse_alarm_topic
from cerberus.events import Severity


# ---------------------------------------------------------------------------
# parse_alarm_topic
# ---------------------------------------------------------------------------

def test_parse_battery_alarm():
    assert parse_alarm_topic(
        "N/0123456789ab/battery/1/Alarms/HighVoltage"
    ) == "battery/1/HighVoltage"


def test_parse_vebus_subpath_alarm():
    assert parse_alarm_topic(
        "N/0123456789ab/vebus/276/Alarms/L1/Overload"
    ) == "vebus/276/L1/Overload"


def test_parse_rejects_non_alarm_topic():
    assert parse_alarm_topic("N/0123456789ab/battery/1/Soc") is None
    assert parse_alarm_topic("N/0123456789ab/heartbeat") is None


def test_parse_rejects_truncated_topic():
    assert parse_alarm_topic("N/0123456789ab/battery/1/Alarms") is None
    assert parse_alarm_topic("Alarms/foo") is None


# ---------------------------------------------------------------------------
# AlarmState edges
# ---------------------------------------------------------------------------

def test_rising_edge_emits_warn():
    s = AlarmState()
    events = s.update("battery/1/HighVoltage", 1, ts=100)
    assert len(events) == 1
    assert events[0].type == "alarm:battery/1/HighVoltage"
    assert events[0].severity == Severity.WARN
    assert events[0].detail["level"] == 1


def test_level_2_emits_crit():
    s = AlarmState()
    events = s.update("battery/1/LowVoltage", 2, ts=100)
    assert len(events) == 1
    assert events[0].severity == Severity.CRIT


def test_same_level_emits_nothing():
    s = AlarmState()
    s.update("battery/1/HighVoltage", 1, ts=100)
    assert s.update("battery/1/HighVoltage", 1, ts=200) == []


def test_falling_edge_emits_cleared():
    s = AlarmState()
    s.update("battery/1/HighVoltage", 1, ts=100)
    events = s.update("battery/1/HighVoltage", 0, ts=200)
    assert len(events) == 1
    assert events[0].type == "alarm:battery/1/HighVoltage_cleared"
    assert events[0].severity == Severity.INFO
    assert events[0].detail["prior_level"] == 1


def test_warn_to_crit_escalation():
    s = AlarmState()
    s.update("battery/1/HighVoltage", 1, ts=100)
    events = s.update("battery/1/HighVoltage", 2, ts=200)
    assert len(events) == 1
    assert events[0].severity == Severity.CRIT
    assert events[0].detail["level"] == 2
    assert events[0].detail["prior_level"] == 1


def test_null_value_ignored():
    """Some vebus alarms publish null when the feature is n/a — must not
    trigger an event or alter state."""
    s = AlarmState()
    s.update("vebus/276/BmsConnectionLost", 1, ts=100)  # establish active
    assert s.active.get("vebus/276/BmsConnectionLost") == 1
    events = s.update("vebus/276/BmsConnectionLost", None, ts=200)
    assert events == []
    # state unchanged
    assert s.active.get("vebus/276/BmsConnectionLost") == 1


def test_zero_when_already_zero_emits_nothing():
    """The Cerbo publishes `{"value":0}` for every alarm on connect (retained).
    We shouldn't emit a flood of cleared events on startup."""
    s = AlarmState()
    for _ in range(5):
        assert s.update("battery/1/HighVoltage", 0, ts=100) == []
    assert s.active == {}


def test_active_set_tracks_concurrent_alarms():
    s = AlarmState()
    s.update("battery/1/HighVoltage", 1, ts=100)
    s.update("vebus/276/Overload", 2, ts=110)
    assert set(s.active) == {"battery/1/HighVoltage", "vebus/276/Overload"}
    s.update("battery/1/HighVoltage", 0, ts=200)
    assert set(s.active) == {"vebus/276/Overload"}
