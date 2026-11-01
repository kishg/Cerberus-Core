"""Bank voltage profile (config.yaml `system`): 12/24/48 V LiFePO4 scaling,
how it fills unset thresholds, and the wrong-nominal warning."""

from __future__ import annotations

import logging
import textwrap
from types import SimpleNamespace

import pytest

from cerberus import config as cfg_mod
from cerberus import detectors as det
from cerberus import drives
from cerberus.__main__ import Cerberus
from cerberus.model import BankState, PackState
from cerberus.profile import DEFAULT_PROFILE, profile

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


def test_12v_profile_is_the_figures_this_rig_was_tuned_on():
    p = DEFAULT_PROFILE
    assert (p.nominal_v, p.chemistry) == (12, "lifepo4")
    assert (p.absorption_v, p.full_min_v, p.peak_limit_v,
            p.over_request_v, p.bulk_max_v, p.crit_v) == (14.4, 14.0, 14.6, 14.45, 14.2, 12.3)
    # And the drive module's own defaults agree with it.
    s = drives.Settings()
    assert (s.peak_limit_v, s.over_request_v, s.bulk_max_v) == (14.6, 14.45, 14.2)


@pytest.mark.parametrize("nominal, k", [(24, 2), (48, 4)])
def test_longer_strings_scale_every_figure(nominal, k):
    p = profile(nominal)
    for name in ("absorption_v", "full_min_v", "peak_limit_v", "over_request_v",
                 "bulk_max_v", "crit_v", "plausible_min_v", "plausible_max_v"):
        assert getattr(p, name) == pytest.approx(getattr(DEFAULT_PROFILE, name) * k)


@pytest.mark.parametrize("system, match", [
    ("      nominal_voltage: 36\n", "nominal voltage"),
    ("      chemistry: agm\n", "not supported yet"),
    ("      nominal_voltage: twelve\n", "system"),
])
def test_unsupported_systems_fail_at_load(tmp_path, monkeypatch, system, match):
    with pytest.raises(cfg_mod.ConfigError, match=match):
        _load(tmp_path, monkeypatch, "    system:\n" + system)


def test_24v_system_fills_unset_voltages(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "    system:\n      nominal_voltage: 24\n")
    assert cfg.system.nominal_v == 24
    assert cfg.solar_path.absorption_v == 28.8
    assert cfg.drives.settings.peak_limit_v == 29.2
    assert cfg.drives.settings.bulk_max_v == 28.4
    ds = det.from_config(cfg.thresholds, cfg.full_charge, {})
    assert ds.low_charge.crit_v == 24.6
    assert ds.full_charge.absorption_v_min == 28.0


def test_explicit_values_beat_the_profile(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, """
    system:
      nominal_voltage: 24
    thresholds:
      low_voltage_crit_v: 25.0
    full_charge:
      absorption_voltage_min: 27.6
    solar_path:
      absorption_v: 28.4
    """)
    ds = det.from_config(cfg.thresholds, cfg.full_charge, {})
    assert ds.low_charge.crit_v == 25.0
    assert ds.full_charge.absorption_v_min == 27.6
    assert cfg.solar_path.absorption_v == 28.4


def test_no_system_block_means_12v(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).system == DEFAULT_PROFILE


def test_drive_limits_follow_the_profile():
    d = drives.Drive(0, 600, "electrical", {"peak_bank_v": 28.0})
    assert drives.evaluate(d, [])                       # 28 V on 12 V limits: flagged
    s24 = drives.Settings(peak_limit_v=29.2, over_request_v=28.9, bulk_max_v=28.4)
    assert not drives.evaluate(d, [], s24)              # normal for 24 V


# --- wrong-nominal warning ------------------------------------------------------

def _bank(volts):
    packs = {i + 1: PackState(pack_id=i + 1, uc="", instance=i + 1, soc=80.0, voltage=v,
                              current=0.0, power=0.0, temperature=20.0, timetogo=None, ts=0)
             for i, v in enumerate(volts)}
    return BankState(ts=0, packs=packs)


def _collector(nominal=12):
    c = Cerberus.__new__(Cerberus)
    c.cfg = SimpleNamespace(system=profile(nominal))
    return c


def test_24v_bank_on_12v_config_warns_once(caplog):
    c = _collector(12)
    with caplog.at_level(logging.WARNING, logger="cerberus"):
        c._check_system_voltage(_bank([26.6]))
        c._check_system_voltage(_bank([26.7]))
    warnings = [r for r in caplog.records if "system.nominal_voltage" in r.message]
    assert len(warnings) == 1 and "26.60 V" in warnings[0].message


@pytest.mark.parametrize("nominal, volts", [
    (12, [13.25, 13.3, 14.75]),   # this rig, including the 09-24 overshoot
    (12, [0.0, 13.3]),            # a missing reading is not evidence
    (24, [26.6]),
])
def test_matching_banks_do_not_warn(caplog, nominal, volts):
    c = _collector(nominal)
    with caplog.at_level(logging.WARNING, logger="cerberus"):
        c._check_system_voltage(_bank(volts))
    assert not [r for r in caplog.records if "system.nominal_voltage" in r.message]
