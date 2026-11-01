"""Bank source: per-pack BMS sum (this rig) vs a single battery monitor.

The monitor payloads below are shaped like system/0/Batteries on a GX with a
SmartShunt (VE.Direct service names) — illustrative, not captured from a van:
this rig has no shunt. The per-pack fixtures are this rig's real captures.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus.model import (
    SOURCE_MONITOR,
    SOURCE_PACKS,
    parse_batteries_array,
    parse_battery_monitor,
)

FIXTURES = Path(__file__).parent / "fixtures"

SHUNT = {
    "id": "com.victronenergy.battery.ttyS5",
    "instance": 279,
    "name": "SmartShunt 500A",
    "soc": 71.4,
    "voltage": 13.21,
    "current": -18.6,
    "power": -245.7,
    "temperature": 18.0,
    "timetogo": 41000,
    "active_battery_service": True,
}
VEBUS = {"id": "com.victronenergy.vebus.ttyS4", "instance": 276, "name": "MultiPlus"}
# A BMS the GX also sees, but which is not the system battery.
SIDE_BMS = {
    "id": "com.victronenergy.battery.socketcan_can0_vi0_uc4242_di0",
    "instance": 512,
    "soc": 90.0,
    "voltage": 13.3,
    "current": -6.0,
    "power": -80.0,
    "temperature": 20.0,
    "active_battery_service": False,
}


def test_monitor_is_the_whole_bank():
    bank = parse_battery_monitor([SHUNT, VEBUS], ts=100)
    assert bank.source == SOURCE_MONITOR
    assert list(bank.packs) == [1]
    m = bank.packs[1]
    assert (m.instance, m.soc, m.voltage, m.timetogo) == (279, 71.4, 13.21, 41000)
    # Bank totals are the monitor's own figures, not a sum of anything.
    assert bank.total_current == pytest.approx(-18.6)
    assert bank.total_power == pytest.approx(-245.7)
    assert bank.soc_spread == 0


def test_monitor_picks_the_active_battery_service():
    bank = parse_battery_monitor([SIDE_BMS, VEBUS, SHUNT])
    assert bank.packs[1].instance == 279


def test_monitor_instance_overrides_the_active_flag():
    bank = parse_battery_monitor([SIDE_BMS, SHUNT], instance=512)
    assert bank.packs[1].instance == 512


def test_lone_battery_without_active_flag_is_used():
    shunt = {k: v for k, v in SHUNT.items() if k != "active_battery_service"}
    assert parse_battery_monitor([shunt, VEBUS]).packs[1].instance == 279


@pytest.mark.parametrize("value, instance", [
    ([], None),                                   # nothing at all
    ([VEBUS], None),                              # inverter only
    ([{**SHUNT, "active_battery_service": False}, SIDE_BMS], None),  # ambiguous
    ([SHUNT], 999),                               # configured instance absent
])
def test_no_unambiguous_monitor_gives_an_empty_bank(value, instance):
    bank = parse_battery_monitor(value, instance=instance)
    assert bank.packs == {} and bank.source == SOURCE_MONITOR


def test_monitor_mode_on_a_per_pack_rig_is_one_pack():
    """Why this rig stays on SOURCE_PACKS: its active service is pack 1 alone,
    so monitor mode would put one pack's figures on the bank."""
    value = json.loads((FIXTURES / "near_full_postdrive.jsonl").read_text().splitlines()[0])["value"]
    packs = parse_batteries_array(value, {"uc1000001": 1, "uc1000002": 2, "uc1000003": 3})
    monitor = parse_battery_monitor(value)
    assert packs.source == SOURCE_PACKS and len(packs.packs) == 3
    assert len(monitor.packs) == 1
    assert monitor.packs[1].instance == packs.packs[1].instance


# --- config ---------------------------------------------------------------------

BASE = """
    mqtt:
      host: venus.local
      portal_id: abc
    storage:
      sqlite_path: /tmp/x.db
"""


def _load(tmp_path, monkeypatch, body):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(BASE + body))
    return cfg_mod.load(p)


def test_default_source_is_packs_and_needs_the_packs_map(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "    packs:\n      uc1: 1\n")
    assert cfg.bank == cfg_mod.BankConfig(source=SOURCE_PACKS, monitor_instance=None)
    with pytest.raises(cfg_mod.ConfigError, match="bank.source: monitor"):
        _load(tmp_path, monkeypatch, "")


def test_monitor_source_needs_no_packs_map(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "    bank:\n      source: monitor\n      monitor_instance: 279\n")
    assert cfg.bank == cfg_mod.BankConfig(source=SOURCE_MONITOR, monitor_instance=279)
    assert cfg.packs == {}


@pytest.mark.parametrize("body, match", [
    ("    bank:\n      source: shunt\n", "bank.source"),
    ("    bank:\n      source: monitor\n      monitor_instance: first\n", "monitor_instance"),
])
def test_bad_bank_config_fails_at_load(tmp_path, monkeypatch, body, match):
    with pytest.raises(cfg_mod.ConfigError, match=match):
        _load(tmp_path, monkeypatch, body)


# --- collector routing -----------------------------------------------------------

def test_collector_stores_the_monitor_as_the_bank(tmp_path):
    from types import SimpleNamespace

    from cerberus.__main__ import Cerberus
    from cerberus.db import Db
    from cerberus.profile import DEFAULT_PROFILE

    published = []
    c = Cerberus.__new__(Cerberus)
    c.cfg = SimpleNamespace(
        bank=cfg_mod.BankConfig(source=SOURCE_MONITOR),
        packs={},
        storage=SimpleNamespace(sample_downsample_seconds=30),
        system=DEFAULT_PROFILE,
    )
    c.db = Db(str(tmp_path / "c.db"))
    c.pack_health = {}
    c.last_sample_ts = 0
    c.detectors = SimpleNamespace(
        evaluate=lambda bank, now: [],
        full_charge=SimpleNamespace(_read_int=lambda key: None,
                                    days_since_full_charge=lambda now: None),
    )
    c.publisher = SimpleNamespace(
        publish_bank_state=published.append,
        publish_full_charge_state=lambda **kw: None,
        publish_event=lambda ev: None,
    )

    c._on_batteries([VEBUS, SIDE_BMS, SHUNT])

    rows = c.db.conn.execute(
        "SELECT pack_id, soc, voltage, current FROM battery_samples").fetchall()
    assert rows == [(1, 71.4, 13.21, -18.6)]
    assert published and published[0].source == SOURCE_MONITOR


# --- derived pack count, capacity, engine current (step 3b) -----------------------

def test_pack_count_follows_the_bank_source(tmp_path, monkeypatch):
    packs = _load(tmp_path, monkeypatch, "    packs:\n      uc1: 1\n      uc2: 2\n      uc3: 3\n")
    assert packs.bank.pack_count == 3
    assert packs.drives.settings.pack_count == 3
    assert packs.solar_path.pack_count == 3 and packs.heater.pack_count == 3
    monitor = _load(tmp_path, monkeypatch, "    bank:\n      source: monitor\n")
    assert monitor.bank.pack_count == 1
    assert monitor.drives.settings.pack_count == 1


def test_engine_rating_reaches_the_solar_path_too(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch,
                "    packs:\n      uc1: 1\n    drives:\n      engine_charge_a: 30\n")
    assert cfg.drives.settings.drive_peak_a == 15.0
    assert cfg.solar_path.charge_on_a == cfg.drives.settings.charge_on_a == 5.0


@pytest.mark.parametrize("body, match", [
    ("    packs:\n      uc1: 1\n    bank:\n      capacity_ah: 0\n", "capacity_ah"),
    ("    packs:\n      uc1: 1\n    bank:\n      capacity_ah: big\n", "capacity_ah"),
    ("    packs:\n      uc1: 1\n    drives:\n      engine_charge_a: -1\n", "engine_charge_a"),
])
def test_bad_sizes_fail_at_load(tmp_path, monkeypatch, body, match):
    with pytest.raises(cfg_mod.ConfigError, match=match):
        _load(tmp_path, monkeypatch, body)
