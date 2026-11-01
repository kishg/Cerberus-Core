"""Per-van analysis values moved out of code into config.yaml.

The repo's own config.yaml must reproduce what the code used to hardcode for
this rig, and a config that omits the sections must get neutral defaults.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus import detectors as det
from cerberus import drives
from cerberus import heater
from cerberus import solar_path

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

MINIMAL = """
    mqtt:
      host: venus.local
      portal_id: abc
    packs:
      uc1: 1
    storage:
      sqlite_path: /tmp/x.db
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_repo_config_keeps_this_rigs_values(monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    cfg = cfg_mod.load(REPO_CONFIG)
    assert cfg.drives.baseline_epochs == ("2026-09-19",)
    assert cfg.bank == cfg_mod.BankConfig(source="packs", monitor_instance=None,
                                          capacity_ah=930.0, pack_count=3)
    assert (cfg.system.nominal_v, cfg.system.chemistry) == (12, "lifepo4")
    s = cfg.drives.settings
    assert (s.peak_limit_v, s.over_request_v, s.bulk_max_v) == (14.6, 14.45, 14.2)
    assert cfg.drives.settings == drives.Settings(
        joint_temp_instance=24, joint_limit_c=70.0, joint_cold_c=32.0,
        peak_limit_v=14.6, over_request_v=14.45, bulk_max_v=14.2,
        pack_count=3, engine_charge_a=200.0, capacity_ah=930.0)
    s = cfg.drives.settings
    assert (s.charge_on_a, s.drive_peak_a, s.step_min_a) == (20.0, 100.0, 40.0)
    assert cfg.solar_path == solar_path.Settings(
        absorption_v=14.4, path_limit_mohm=75.0, process_after_hour=21,
        pack_count=3, charge_on_a=20.0)
    assert cfg.heater == heater.Settings(
        on_delta_w=800, off_delta_w=400, baseline_max_w=800, default_baseline_w=150,
        pack_count=3)
    lc = det.from_config(cfg.thresholds, cfg.full_charge, {}).low_charge
    assert lc.crit_v == 12.3


def test_omitted_sections_get_neutral_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    cfg = cfg_mod.load(_write(tmp_path, MINIMAL))
    assert cfg.drives.baseline_epochs == ()
    assert cfg.drives.settings.joint_temp_instance is None
    assert cfg.solar_path == solar_path.DEFAULT_SETTINGS
    assert cfg.heater == heater.DEFAULT_SETTINGS


def test_crit_voltage_from_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    cfg = cfg_mod.load(_write(tmp_path, MINIMAL + """
    thresholds:
      low_voltage_crit_v: 24.6
    """))
    assert det.from_config(cfg.thresholds, cfg.full_charge, {}).low_charge.crit_v == 24.6


def test_load_analysis_needs_no_password(monkeypatch):
    monkeypatch.delenv("CERBERUS_MQTT_PASSWORD", raising=False)
    drives_cfg, sp, tz = cfg_mod.load_analysis(REPO_CONFIG)
    assert tz is None     # this rig follows the GX's own setting
    assert drives_cfg.settings.joint_temp_instance == 24
    assert sp.absorption_v == 14.4


@pytest.mark.parametrize("extra, match", [
    ("solar_path:\n  process_after_hour: 24\n", "process_after_hour"),
    ("solar_path:\n  absorption_v: high\n", "solar_path"),
    ("heater:\n  on_delta_w: 400\n  off_delta_w: 400\n", "off_delta_w"),
    ("drives:\n  joint_temp_instance: probe\n", "drives"),
])
def test_bad_values_fail_at_load(tmp_path, monkeypatch, extra, match):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    with pytest.raises(cfg_mod.ConfigError, match=match):
        cfg_mod.load(_write(tmp_path, MINIMAL + textwrap.indent(extra, "    ")))


# --- bank.charge_on_a (site-agnostic plan, PR 2) ---------------------------------

def test_charge_on_a_is_derived_from_the_engine_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    cfg = cfg_mod.load(REPO_CONFIG)                     # engine_charge_a 200 A
    assert cfg.bank.charge_on_a == cfg.drives.settings.charge_on_a == 20.0
    assert cfg.solar_path.charge_on_a == 20.0
    assert cfg.thresholds["solar_open_charging_guard_a"] == 20.0
    assert cfg_mod.load_ui(REPO_CONFIG).charge_on_a == 20.0
    small = cfg_mod.load(_write(tmp_path, MINIMAL + "    drives:\n      engine_charge_a: 100\n"))
    assert small.bank.charge_on_a == 10.0
    tiny = cfg_mod.load(_write(tmp_path, MINIMAL + "    drives:\n      engine_charge_a: 20\n"))
    assert tiny.bank.charge_on_a == 5.0                 # the floor


def test_bank_charge_on_a_overrides_everything_but_drive_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    p = _write(tmp_path, MINIMAL + "    bank:\n      charge_on_a: 30\n")
    cfg = cfg_mod.load(p)
    assert cfg.bank.charge_on_a == cfg.solar_path.charge_on_a == 30.0
    assert cfg.thresholds["solar_open_charging_guard_a"] == 30.0
    assert cfg_mod.load_ui(p).charge_on_a == 30.0
    assert cfg_mod.load_analysis(p)[1].charge_on_a == 30.0
    assert cfg.drives.settings.charge_on_a == 20.0      # drives stay engine-based
    assert cfg.solar_path.pack_count == cfg.bank.pack_count == 1


@pytest.mark.parametrize("value", [0, -5, "lots"])
def test_bank_charge_on_a_must_be_a_positive_number(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    with pytest.raises(cfg_mod.ConfigError, match="bank.charge_on_a"):
        cfg_mod.load(_write(tmp_path, MINIMAL + f"    bank:\n      charge_on_a: {value}\n"))
