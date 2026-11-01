from __future__ import annotations

import json
from pathlib import Path

import pytest

from cerberus.model import parse_batteries_array

PACKS = {
    "uc1000001": 1,
    "uc1000002": 2,
    "uc1000003": 3,
}

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    """Read a JSONL fixture; each line is {"value": [...]}. Return list of values."""
    out: list[dict] = []
    with (FIXTURES / name).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            out.append(payload["value"])
    return out


def test_parse_filters_vebus_and_maps_packs_by_uc():
    """The Batteries array mixes battery elements with the vebus/MultiPlus
    element. The inverter element must be filtered out (wrong id prefix,
    no soc field), and packs must be keyed off the uc token in the id."""
    values = _load("near_full_postdrive.jsonl")
    assert values, "fixture is empty"

    bank = parse_batteries_array(values[0], PACKS, ts=1780874956)

    assert set(bank.packs.keys()) == {1, 2, 3}
    assert bank.packs[1].uc == "uc1000001"
    assert bank.packs[2].uc == "uc1000002"
    assert bank.packs[3].uc == "uc1000003"
    assert bank.packs[1].instance == 1
    assert bank.packs[2].instance == 2
    assert bank.packs[3].instance == 3
    assert bank.packs[1].soc == 96.0
    assert bank.packs[2].soc == 98.0
    assert bank.packs[3].soc == 61.0
    assert bank.soc_spread == pytest.approx(37.0)


def test_parse_drift_fixture():
    """drift_discharging fixture: packs 62/64/25, ~13.15V, discharging.
    Spread 39 — well above the 15% threshold. Currents negative (discharge)."""
    values = _load("drift_discharging.jsonl")
    assert values

    bank = parse_batteries_array(values[0], PACKS)
    assert set(bank.packs.keys()) == {1, 2, 3}
    assert bank.packs[1].soc == 62.0
    assert bank.packs[2].soc == 64.0
    assert bank.packs[3].soc == 25.0
    assert bank.soc_spread == pytest.approx(39.0)
    # all packs discharging
    assert all(p.current < 0 for p in bank.packs.values())


def test_parse_skips_unmapped_uc():
    value = [
        {
            "id": "com.victronenergy.battery.socketcan_can0_vi9_uc9999999_di9",
            "instance": 9,
            "soc": 50.0,
            "voltage": 13.0,
            "current": -1.0,
            "power": -13,
            "temperature": 28.0,
            "timetogo": 100000,
        }
    ]
    bank = parse_batteries_array(value, PACKS)
    assert bank.packs == {}


def test_parse_handles_missing_timetogo():
    value = [
        {
            "id": "com.victronenergy.battery.socketcan_can0_vi1_uc1000001_di1",
            "instance": 1,
            "soc": 90.0,
            "voltage": 13.4,
            "current": 0.0,
            "power": 0,
            "temperature": 30.0,
        }
    ]
    bank = parse_batteries_array(value, PACKS)
    assert bank.packs[1].timetogo is None


def test_all_fixture_messages_parse_to_three_packs():
    """Every Batteries message in both real captures should yield 3 mapped packs."""
    for name in ("drift_discharging.jsonl", "near_full_postdrive.jsonl"):
        for i, value in enumerate(_load(name)):
            bank = parse_batteries_array(value, PACKS)
            assert set(bank.packs.keys()) == {1, 2, 3}, f"{name} line {i}"


def _bank_at_alternator_peak():
    """The real 2026-06-16 07:41:10 sample — peak alternator charge via the ACR.

    Per-pack currents are exactly as captured; powers sum to the 2214 W the
    packs actually reported.
    """
    value = [
        {
            "id": "com.victronenergy.battery.socketcan_can0_vi1_uc1000001_di1",
            "instance": 1,
            "soc": 88.0,
            "voltage": 13.3,
            "current": 53.25,
            "power": 708,
            "temperature": 29.0,
            "active_battery_service": True,
        },
        {
            "id": "com.victronenergy.battery.socketcan_can0_vi2_uc1000002_di2",
            "instance": 2,
            "soc": 88.0,
            "voltage": 13.3,
            "current": 53.00,
            "power": 750,
            "temperature": 29.0,
            "active_battery_service": False,
        },
        {
            "id": "com.victronenergy.battery.socketcan_can0_vi3_uc1000003_di3",
            "instance": 3,
            "soc": 88.0,
            "voltage": 13.3,
            "current": 60.12,
            "power": 756,
            "temperature": 29.0,
            "active_battery_service": False,
        },
    ]
    return parse_batteries_array(value, PACKS)


def test_bank_totals_sum_all_packs():
    bank = _bank_at_alternator_peak()
    assert bank.total_current == pytest.approx(166.37, abs=0.01)
    assert bank.total_power == pytest.approx(2214.0, abs=0.5)


def test_bank_power_is_not_the_active_service_pack():
    """Regression: the Cerbo's system/0/Dc/Battery/Power reports ONLY the pack
    holding active_battery_service (pack 1), which understated this bank by ~3x.
    HA's Bank Power Net was wired to that topic and read 698 W while the bank
    was actually taking 2214 W from the alternator. Bank totals must never
    collapse to a single pack.
    """
    bank = _bank_at_alternator_peak()
    active_pack_power = bank.packs[1].power
    assert active_pack_power == 708
    assert bank.total_power != active_pack_power
    # the understatement that started the whole investigation
    assert bank.total_power / active_pack_power == pytest.approx(3.13, abs=0.05)
