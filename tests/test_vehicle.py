"""Vehicle values imported through the HA broker (cerberus.vehicle): the
chassis's own cloud data, e.g. Mercedes me via Home Assistant."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cerberus import config as cfg_mod
from cerberus import findings as F
from cerberus import vehicle
from cerberus.db import Db
from cerberus.ha_mqtt import HaBroker
from cerberus.ui import api

NY = ZoneInfo("America/New_York")
NOW = int(datetime(2026, 11, 1, 18, 0, tzinfo=NY).timestamp())
DAY = 86400
S = F.Settings(tz=NY)

# The van's real snapshot, 2026-11-01 18:04 (Mercedes me via HA).
VAN = {"ts": NOW, "source": "mercedes_me", "values": {
    "fuel_level_pct": 65.0, "range_mi": 237.0, "def_level_pct": 78.0, "odometer_mi": 36960.0,
    "tire_fl_psi": 55.0, "tire_fr_psi": 55.0, "tire_rl_psi": 65.0, "tire_rr_psi": 66.0,
    "starter_battery_state": 0.0, "ignition_state": 0.0, "lock_state": 2.0, "tire_warning": 0,
    "coolant_low": 0, "brake_fluid_low": 0, "washer_low": 0, "engine_light": 0}}


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "v.db"))


def _keys(db, now=NOW):
    return {f.key: f for f in F.derive(db.conn, S, now)}


def _snap(db, ts, **over):
    vals = {**VAN["values"], **over}
    db.insert_vehicle_samples(ts, "mercedes_me", vals)


def test_parse_the_vans_snapshot():
    ts, source, values = vehicle.parse_snapshot(json.dumps(VAN), now=NOW)
    assert (ts, source) == (NOW, "mercedes_me")
    assert values["tire_rr_psi"] == 66.0 and values["coolant_low"] == 0.0


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"values": 3}', b'{"ts": 1}'])
def test_parse_rejects_what_isnt_a_snapshot(payload):
    assert vehicle.parse_snapshot(payload, now=NOW) is None


def test_parse_drops_text_and_fixes_a_wild_clock():
    ts, _, values = vehicle.parse_snapshot(
        json.dumps({"ts": 5, "values": {"a": 1, "b": "x", "c": True}}), now=NOW)
    assert ts == NOW and values == {"a": 1.0, "c": 1.0}


def test_the_vans_snapshot_raises_no_findings(db):
    _snap(db, NOW - 600)
    found = _keys(db)
    assert not {"starter_battery", "tyre_pressure", "vehicle_fluids"} & set(found)


@pytest.mark.parametrize("state,sev,word", [(1, "info", "fair"), (3, "attention", "weak"),
                                            (2, "urgent", "poor")])
def test_starter_battery_by_the_vans_rating(db, state, sev, word):
    _snap(db, NOW - 600, starter_battery_state=state)
    f = _keys(db)["starter_battery"]
    assert (f.status, f.severity) == ("open", sev) and word in f.explain


def test_starter_battery_resolves(db):
    _snap(db, NOW - 2 * DAY, starter_battery_state=3)
    _snap(db, NOW - DAY, starter_battery_state=0)
    assert _keys(db)["starter_battery"].status == "resolved"


HOT = {"tire_fl_psi": 52.0, "tire_fr_psi": 52.0, "tire_rl_psi": 61.0, "tire_rr_psi": 62.0}


def _drive(db, start, minutes=20, cold=-5.0, **hot):
    """A drive: a cold-start reading `cold` psi under HOT, then the end-of-drive
    reading (HOT with overrides) after `minutes`, then ignition off."""
    end = start + minutes * 60
    _snap(db, start, ignition_state=4, **{k: v + cold for k, v in HOT.items()})
    _snap(db, end, ignition_state=4, **{**HOT, **hot})
    _snap(db, end + 60, ignition_state=0, **{**HOT, **hot})


def _tyre(db):
    return _keys(db).get("tyre_pressure")


def test_tyre_low_against_its_hot_readings(db):
    for k in range(3):
        _drive(db, NOW - (10 - k) * DAY)
    _drive(db, NOW - 3600, tire_rl_psi=54.0)            # 11% under 61 hot
    f = _tyre(db)
    assert f.status == "open"
    assert "rear left is at 54 psi, against 61 psi usually at the end of a drive" in f.explain


def test_cold_starts_and_parked_repeats_are_not_a_drop(db):
    # 2026-11-09: the import repeated 65/66 psi while parked, and the cold start
    # read 8-9 psi under the hot end-of-drive values. Neither is a leak.
    for k in range(6):
        _snap(db, NOW - (20 - k) * DAY)
    for k in range(3):
        _drive(db, NOW - (10 - k) * DAY)
    _drive(db, NOW - 3600, cold=-8.0, tire_rl_psi=59.0)
    _snap(db, NOW - 600, ignition_state=4, **{k: v - 8.0 for k, v in HOT.items()})
    assert _tyre(db) is None


def test_a_reading_far_under_hot_is_an_anomaly_even_cold(db):
    _drive(db, NOW - 2 * DAY)
    _snap(db, NOW - 600, ignition_state=4, tire_rr_psi=46.0)  # 16 under 62 hot
    f = _tyre(db)
    assert f.status == "open" and "rear right is at 46 psi, against 62 psi" in f.explain


def test_short_drives_give_no_hot_reading(db):
    for k in range(4):
        _drive(db, NOW - (10 - k) * DAY, minutes=8)
    _snap(db, NOW - 600, ignition_state=4, tire_rr_psi=40.0)
    assert _tyre(db) is None                             # no hot baseline yet


def test_tyre_warning_flag(db):
    _snap(db, NOW - 600, tire_warning=1)
    f = _keys(db)["tyre_pressure"]
    assert f.status == "open" and "warning is on" in f.explain


def test_fluids(db):
    _snap(db, NOW - 600, coolant_low=1, washer_low=1)
    f = _keys(db)["vehicle_fluids"]
    assert (f.status, f.severity) == ("open", "attention")
    assert "coolant" in f.explain and "washer fluid" in f.explain


def test_now_takes_the_newer_of_obd_and_import(db):
    db.conn.execute("INSERT INTO obd_samples (ts, ecu, name, raw, value) "
                    "VALUES (?, '7DF', 'fuel_level_pct', '', 70.0)", (NOW - 3 * 3600,))
    _snap(db, NOW - 600)
    v = api.chassis_view(db.conn, api.Context(), NOW)["vehicle"]
    assert v["fuel"] == {"ts": NOW - 600, "pct": 65, "source": "import", "range_mi": 237}
    assert v["odometer"]["mi"] == 36960 and v["def"]["source"] == "import"


def test_config_import_topics():
    import yaml
    from pathlib import Path
    base = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    raw = cfg_mod.merge(base, {"ha_mqtt": {"enabled": True,
                                           "import_topics": ["cerberus/import/mercedes", " "]}})
    assert cfg_mod.from_raw(raw, password="x").ha_mqtt.import_topics == ("cerberus/import/mercedes",)
    assert cfg_mod.from_raw(base, password="x").ha_mqtt.import_topics == ()


def test_ha_broker_subscribes_on_connect_and_passes_messages():
    got = []
    b = HaBroker("127.0.0.1", 1883, "cerberus/availability",
                 subscriptions=("cerberus/import/mercedes",),
                 on_message=lambda t, p: got.append((t, p)))

    class Client:
        subs = []

        def subscribe(self, topic, qos=0):
            self.subs.append(topic)

    c = Client()
    b._on_connect(c, None, None, 0)
    assert c.subs == ["cerberus/import/mercedes"]

    class Msg:
        topic, payload = "cerberus/import/mercedes", b"{}"

    b._on_message(c, None, Msg())
    assert got == [("cerberus/import/mercedes", b"{}")]
