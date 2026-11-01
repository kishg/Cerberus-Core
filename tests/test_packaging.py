"""Step 5.6b: a new box runs out of the box — weather defaults, waiting
services, and the install script and units agreeing on paths."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus.firstrun import _config_text
from cerberus.location import VanLocation

ROOT = Path(__file__).resolve().parents[1]


def _fresh(**extra):
    import yaml
    raw = yaml.safe_load(_config_text(["10.0.0.5"], "abc", "/var/lib/cerberus/cerberus.db"))
    raw.update({"packs": {"uc1": 1}}, **extra)
    return cfg_mod.from_raw(raw, password="x")


def test_weather_on_by_default_with_an_identifying_agent():
    w = _fresh().weather
    assert w.enabled and "github.com/kishg/Cerberus-Core" in w.user_agent


def test_weather_without_a_position_loads_and_is_skipped(monkeypatch):
    from cerberus.__main__ import Cerberus
    calls = []
    monkeypatch.setattr("cerberus.__main__.fetch_weather", lambda *a, **k: calls.append(a))
    c = Cerberus.__new__(Cerberus)
    c.cfg, c.location, c.last_weather_tick_ts = _fresh(), VanLocation(), 0
    c._weather_tick()
    assert calls == []                                  # nothing fetched for 0, 0
    c.location.phone_fix(39.77, -86.16, 2_000_000_000)
    c.last_weather_tick_ts = 0
    c._weather_tick()
    assert calls and calls[0][:2] == (39.77, -86.16)


def test_this_vans_weather_is_unchanged():
    w = cfg_mod.from_raw(cfg_mod.read_raw(ROOT / "config.yaml"), password="x").weather
    assert w.enabled and w.user_agent.startswith("Cerberus/0.1 (van battery telemetry")


def test_ble_sidecar_waits_on_a_box_not_set_up(tmp_path):
    """Exits 0 once told to stop, instead of 2 straight away (a restart loop)."""
    log = tmp_path / "ble.log"
    with open(log, "w") as out:
        p = subprocess.Popen([sys.executable, "-m", "cerberus.ble",
                              "--config", str(tmp_path / "config.yaml")],
                             stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while "waiting" not in log.read_text() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert "waiting for first-run setup" in log.read_text() and p.poll() is None
        finally:
            p.terminate()
            code = p.wait(10)
    assert code == 0


@pytest.mark.parametrize("unit", ["cerberus.service", "cerberus-ui.service", "cerberus-ble.service"])
def test_units_share_paths_with_the_install_script(unit):
    text = (ROOT / "systemd" / unit).read_text()
    script = (ROOT / "bin/install-linux.sh").read_text()
    assert "--config /etc/cerberus/config.yaml" in text and "ETC=/etc/cerberus" in script
    assert "CERBERUS_SQLITE_PATH=/var/lib/cerberus/cerberus.db" in text and "STATE=/var/lib/cerberus" in script
    assert "User=cerberus" in text and unit in script


def test_install_script_parses():
    assert subprocess.run(["bash", "-n", str(ROOT / "bin/install-linux.sh")]).returncode == 0
