"""Victron settings history (cerberus.device_settings).

The GX publishes ~950 settings on the feed Cerberus already reads. Pinned
here: which paths are kept, that only first sightings and changes are written,
that a null (device off) is never a change, that a chatty path is muted, the
expect check and its finding, and that settings topics stay out of the AC
branch (BatteryLife/State ends in /State).
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from cerberus import findings
from cerberus.db import Db
from cerberus.device_settings import SettingsRecorder, label, path_of, same
from cerberus.mqtt_client import CerboMqtt

N = "N/0123456789ab/"
SVS = "settings/0/Settings/SystemSetup/SharedVoltageSense"
MPPT_LIMIT = "solarcharger/277/Settings/ChargeCurrentLimit"


def _db():
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def _rows(db):
    return [(ts, p, json.loads(v)) for ts, p, v in db.conn.execute(
        "SELECT ts, path, value FROM device_settings ORDER BY ts, path")]


def _events(db, kind="victron_%"):
    return [(t, json.loads(d)["detail"]) for t, d in db.conn.execute(
        "SELECT type, detail FROM events WHERE type LIKE ? ORDER BY rowid", (kind,))]


@pytest.mark.parametrize("path", [
    SVS, "settings/0/Settings/Alarm/Vebus/LowBattery", "settings/0/Settings/Services/Bol",
    MPPT_LIMIT, "solarcharger/277/Mode", "solarcharger/277/FirmwareVersion",
    "vebus/276/Devices/0/Settings/PowerAssistEnabled", "vebus/276/Devices/0/FirmwareVersion",
    "vebus/276/Ac/In/1/CurrentLimit", "battery/2/Info/MaxChargeVoltage",
    "battery/1/FirmwareVersion", "platform/0/Firmware/Installed/Version",
])
def test_settings_paths_are_kept(path):
    assert path_of(N + path) == path


@pytest.mark.parametrize("path", [
    "system/0/Dc/Battery/Voltage", "solarcharger/277/Yield/Power", "vebus/276/Mode",
    "vebus/276/Ac/ActiveIn/L1/P", "battery/1/Soc", "battery/1/Info/ChargeRequest",
    "settings/0/Settings/Generator0/AccumulatedTotal",
    "settings/0/Settings/CGwacs/BatteryLife/State",
    "settings/0/Settings/DynamicEss/BatteryChargeLimit",
])
def test_measurements_and_counters_are_not(path):
    assert path_of(N + path) is None


def test_first_sighting_and_changes_only():
    db = _db()
    r = SettingsRecorder(db)
    r.on_message(N + SVS, 1, now=100)
    r.on_message(N + SVS, 1, now=400)          # the 5-minute full republish
    r.on_message(N + SVS, 0, now=500)
    r.on_message(N + "system/0/Dc/Battery/Voltage", 13.2, now=500)
    assert _rows(db) == [(100, SVS, 1), (500, SVS, 0)]
    assert _events(db) == [("victron_setting_changed", {"path": SVS, "old": 1, "new": 0})]


def test_null_is_not_a_change():
    """With the inverter off the VE.Bus tree reads null; that is not a setting."""
    db = _db()
    r = SettingsRecorder(db)
    p = "vebus/276/Devices/0/Settings/PowerAssistEnabled"
    r.on_message(N + p, True, now=100)
    r.on_message(N + p, None, now=200)
    r.on_message(N + p, True, now=300)
    assert _rows(db) == [(100, p, True)] and _events(db) == []


def test_float_noise_is_not_a_change():
    db = _db()
    r = SettingsRecorder(db)
    p = "battery/1/Info/MaxChargeVoltage"
    r.on_message(N + p, 14.399999618530273, now=100)
    r.on_message(N + p, 14.4, now=200)
    assert len(_rows(db)) == 1 and same(14.4, 14.3999996) and not same(1, True)


def test_history_survives_a_restart():
    db = _db()
    SettingsRecorder(db).on_message(N + MPPT_LIMIT, 50.0, now=100)
    r = SettingsRecorder(db)                    # collector restarted
    r.on_message(N + MPPT_LIMIT, 50.0, now=200)
    r.on_message(N + MPPT_LIMIT, 35.0, now=300)
    assert _rows(db) == [(100, MPPT_LIMIT, 50.0), (300, MPPT_LIMIT, 35.0)]
    assert _events(db)[0][1] == {"path": MPPT_LIMIT, "old": 50.0, "new": 35.0}


def test_a_chatty_path_is_muted():
    db = _db()
    r = SettingsRecorder(db)
    p = "settings/0/Settings/Gui/Brightness"
    for i in range(30):
        r.on_message(N + p, i, now=100 + i)
    # The first sighting and 10 changes, then silence for the run.
    assert len(_rows(db)) == 11
    r.on_message(N + p, 999, now=10_000)
    assert len(_rows(db)) == 11


def test_expect_raises_updates_and_clears():
    db = _db()
    r = SettingsRecorder(db, {SVS: 1, MPPT_LIMIT: 50})
    r.on_message(N + SVS, 1, now=100)
    r.on_message(N + MPPT_LIMIT, 50.0, now=100)
    assert _events(db, "victron_settings%") == []
    r.on_message(N + SVS, 0, now=200)
    r.on_message(N + MPPT_LIMIT, 35.0, now=300)
    r.on_message(N + SVS, 1, now=400)
    r.on_message(N + MPPT_LIMIT, 50.0, now=500)
    assert _events(db, "victron_settings%") == [
        ("victron_settings_unexpected", {"off": {SVS: [1, 0]}}),
        ("victron_settings_unexpected", {"off": {SVS: [1, 0], MPPT_LIMIT: [50, 35.0]}}),
        ("victron_settings_unexpected", {"off": {MPPT_LIMIT: [50, 35.0]}}),
        ("victron_settings_unexpected_cleared", {}),
    ]


def test_unexpected_setting_is_a_finding():
    db = _db()
    r = SettingsRecorder(db, {SVS: 1})
    r.on_message(N + SVS, 0, now=1000)
    out = [f for f in findings.derive(db.conn, findings.Settings(), 1100)
           if f.key == "victron_settings"]
    assert len(out) == 1 and out[0].status == findings.OPEN
    assert "Shared voltage sense (DVCC) is 0, expected 1" in out[0].explain


def test_labels():
    assert label(MPPT_LIMIT) == "MPPT 277 Settings/ChargeCurrentLimit"
    assert label("settings/0/Settings/Relay/Function") == "Relay/Function"


class _Msg:
    def __init__(self, topic, value):
        self.topic = topic
        self.payload = json.dumps({"value": value}).encode()


class _MqttCfg:
    primary_topic = "system/0/Batteries"


def _dispatcher(calls):
    m = CerboMqtt.__new__(CerboMqtt)
    m.cfg = _MqttCfg()
    m.on_dc = m.on_wifi = m.on_solar = m.on_temperature = None
    m.on_battery_health = m.on_alarm = None
    m.on_ac = lambda t, v: calls.append(("ac", t))
    m.on_location = lambda t, v: calls.append(("location", t))
    m.on_setting = lambda t, v: calls.append(("setting", t))
    return m


def test_settings_topics_stay_out_of_the_ac_branch():
    calls = []
    m = _dispatcher(calls)
    m._on_message(None, None, _Msg(N + "settings/0/Settings/CGwacs/BatteryLife/State", 1))
    m._on_message(None, None, _Msg(N + "settings/0/Settings/System/TimeZone", "UTC"))
    m._on_message(None, None, _Msg(N + "vebus/276/Mode", 3))
    assert calls == [
        ("setting", N + "settings/0/Settings/CGwacs/BatteryLife/State"),
        ("setting", N + "settings/0/Settings/System/TimeZone"),
        ("location", N + "settings/0/Settings/System/TimeZone"),
        ("setting", N + "vebus/276/Mode"),
        ("ac", N + "vebus/276/Mode"),
    ]


def test_config(tmp_path, monkeypatch):
    from cerberus import config as cfg_mod
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "x")
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    plain = tmp_path / "p.yaml"
    plain.write_text(src)
    v = cfg_mod.load(plain).victron
    assert v.record_settings is True and v.expect == {}
    on = tmp_path / "on.yaml"
    on.write_text(src.replace("  # expect:\n  #   settings/0/Settings/SystemSetup/SharedVoltageSense: 1",
                              "  expect:\n    settings/0/Settings/SystemSetup/SharedVoltageSense: 1"))
    assert cfg_mod.load(on).victron.expect == {SVS: 1}
    bad = tmp_path / "bad.yaml"
    bad.write_text(on.read_text().replace("SharedVoltageSense: 1", "SharedVoltageSense: [1]"))
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod.load(bad)
