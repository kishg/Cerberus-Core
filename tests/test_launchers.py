"""bin/run-*.sh: CERBERUS_HOME points the launchers at a deployment folder
(.env, config.yaml, soak/, .venv) apart from the code. A fake venv python
records what each launcher would run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"

FAKE_PY = """#!/bin/bash
echo "ARGS $*"
echo "DB $CERBERUS_SQLITE_PATH"
echo "PW ${CERBERUS_MQTT_PASSWORD:-}"
echo "PWD $(pwd)"
"""


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "van"
    (h / ".venv" / "bin").mkdir(parents=True)
    py = h / ".venv" / "bin" / "python"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    (h / ".env").write_text("CERBERUS_MQTT_PASSWORD=from-the-van\n")
    return h


@pytest.mark.parametrize("script, module", [("run-collector.sh", "cerberus"),
                                            ("run-ui.sh", "cerberus.ui"),
                                            ("run-ble.sh", "cerberus.ble")])
def test_cerberus_home_supplies_config_env_database_and_venv(home, script, module):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERBERUS_")}
    env.update(CERBERUS_HOME=str(home), CERBERUS_PREVENT_SLEEP="0")
    out = subprocess.run(["bash", str(BIN / script)], env=env, capture_output=True,
                         text=True, check=True).stdout
    assert f"ARGS -m {module} --config {home}/config.yaml" in out
    assert f"DB {home}/soak/data/cerberus.db" in out
    assert f"PWD {home}" in out
    if script != "run-ui.sh":                        # the UI needs no secrets
        assert "PW from-the-van" in out
