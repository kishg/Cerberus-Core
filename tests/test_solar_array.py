"""Solar-open thresholds relative to the array (config.yaml `solar`, step 3c).

The strongest check is the replay: the real 2026-08-29 trip and alternator
false positive, with every PV voltage doubled as if the panels were wired two
in series, must give exactly the same events on a 40 V array config as the
originals do on this rig's 20 V one.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import detectors as det
from cerberus.__main__ import Cerberus

from .test_detectors import FIXTURES, _bank_at, _full_bank, _load_solar_trip, _solar_at

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
SOLAR_KEYS = ("solar_open_pv_voltage_min", "solar_open_circuit_voltage",
              "solar_open_recent_power_w", "solar_open_charging_guard_a")

BASE = """
    mqtt:
      host: venus.local
      portal_id: abc
    packs:
      uc1: 1
    storage:
      sqlite_path: /tmp/x.db
"""


def _load(tmp_path, monkeypatch, body=""):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(BASE + body))
    return cfg_mod.load(p)


def _detector(thresholds) -> det.SolarOutputOpenDetector:
    """Built from thresholds exactly as the collector builds it."""
    return det.SolarOutputOpenDetector(
        pv_voltage_min=thresholds["solar_open_pv_voltage_min"],
        open_circuit_voltage=thresholds["solar_open_circuit_voltage"],
        recent_power_w=thresholds["solar_open_recent_power_w"],
        charging_guard_a=thresholds["solar_open_charging_guard_a"],
        sustain_seconds=90,
    )


def test_this_rigs_array_derives_its_tuned_thresholds(tmp_path, monkeypatch):
    """Delete the four hand-tuned keys from the repo config and the `solar`
    block must regenerate them exactly."""
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    tuned = cfg_mod.load(REPO_CONFIG).thresholds
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    for k in SOLAR_KEYS:
        raw["thresholds"].pop(k)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    derived = cfg_mod.load(p).thresholds
    for k in SOLAR_KEYS:
        assert derived[k] == tuned[k] == {
            "solar_open_pv_voltage_min": 15.0, "solar_open_circuit_voltage": 18.5,
            "solar_open_recent_power_w": 25.0, "solar_open_charging_guard_a": 20.0,
        }[k], k


def test_series_string_scales_voltage_and_size_scales_power(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "    solar:\n      array_voc_v: 66\n      array_w: 600\n")
    t = cfg.thresholds
    assert (t["solar_open_pv_voltage_min"], t["solar_open_circuit_voltage"],
            t["solar_open_recent_power_w"]) == (49.5, 61.05, 50.0)


def test_explicit_threshold_wins(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
    solar:
      array_voc_v: 40
    thresholds:
      solar_open_circuit_voltage: 38.0
    """)
    assert cfg.thresholds["solar_open_circuit_voltage"] == 38.0
    assert cfg.thresholds["solar_open_pv_voltage_min"] == 30.0


def test_charging_guard_follows_the_engine_rating(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "    drives:\n      engine_charge_a: 30\n")
    assert cfg.thresholds["solar_open_charging_guard_a"] == 5.0


@pytest.mark.parametrize("body", [
    "    solar:\n      array_voc_v: 0\n",
    "    solar:\n      array_w: -100\n",
    "    solar:\n      array_voc_v: high\n",
])
def test_bad_array_fails_at_load(tmp_path, monkeypatch, body):
    with pytest.raises(cfg_mod.ConfigError, match="solar"):
        _load(tmp_path, monkeypatch, body)


# --- replay ---------------------------------------------------------------------

def _replay(rows, thresholds, k=1.0, bank_for=lambda r: _full_bank()):
    d = _detector(thresholds)
    out = []
    for r in rows:
        solar = _solar_at(r["pv_power"], r["pv_voltage"] * k, r["charge_current"],
                          r["state"], r["error_code"])
        out += [(e.type, e.ts) for e in d.evaluate(solar, bank_for(r), r["ts"])]
    return out


def _thresholds(voc):
    t = det.solar_open_thresholds(voc, 300.0)
    t["solar_open_charging_guard_a"] = 20.0
    return t


def test_real_trip_replays_identically_on_a_two_series_array():
    rows = _load_solar_trip()
    original = _replay(rows, _thresholds(20.0))
    assert original == [("solar_output_open", 1788010147), ("solar_output_open_cleared", 1788010359)]
    assert _replay(rows, _thresholds(40.0), k=2.0) == original
    # And the reason the setting matters: the 40 V array on 20 V thresholds
    # holds PV above "sun is up" all through twilight, the 09-18 failure mode.
    assert _thresholds(20.0)["solar_open_circuit_voltage"] < 2.0 * 17.2


def test_alternator_false_positive_stays_silent_on_a_two_series_array():
    import json
    rows = [json.loads(line) for line in
            (FIXTURES / "solar_float_false_positive_2026_08_29.jsonl").read_text().splitlines()
            if line.strip()]
    bank = lambda r: _bank_at(r["bank_a"], r["min_soc"])
    assert _replay(rows, _thresholds(20.0), bank_for=bank) == []
    assert _replay(rows, _thresholds(40.0), k=2.0, bank_for=bank) == []


# --- wrong-Voc warning ----------------------------------------------------------

def _collector(voc, pv_v):
    c = Cerberus.__new__(Cerberus)
    c.cfg = SimpleNamespace(solar=cfg_mod.SolarArrayConfig(array_voc_v=voc))
    c.solar = SimpleNamespace(primary=SimpleNamespace(pv_voltage=pv_v))
    return c


def _warnings(caplog):
    return [r for r in caplog.records if "solar.array_voc_v" in r.message]


def test_series_string_on_the_default_warns_once(caplog):
    c = _collector(20.0, 61.2)
    with caplog.at_level(logging.WARNING, logger="cerberus"):
        c._check_array_voltage()
        c._check_array_voltage()
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("voc, pv_v", [
    (20.0, 21.93),    # this rig's coldest morning
    (20.0, None),     # no reading yet
    (66.0, 61.2),
])
def test_plausible_pv_does_not_warn(caplog, voc, pv_v):
    with caplog.at_level(logging.WARNING, logger="cerberus"):
        _collector(voc, pv_v)._check_array_voltage()
    assert not _warnings(caplog)
