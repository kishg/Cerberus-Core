"""Step 5.0: moving this Cerberus to the mini PC (cerberus.migrate,
tools/compare_boxes.py)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import envfile, migrate, notes, phonefix, vcsf, victron_imports
from cerberus.db import Db
from cerberus.ui import auth

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def old_box(tmp_path):
    """A copy of this van's config with the files a running box has."""
    box = tmp_path / "old"
    box.mkdir()
    cfg = box / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfg)
    (box / "setup.yaml").write_text("weather:\n  zip_code: '07054'\n  latitude: 40.86\n  longitude: -74.41\n")
    envfile.set_value(envfile.path_for(cfg), envfile.MQTT_PASSWORD, "pw")
    auth.set_pin(cfg, "2468")
    auth.Sessions(cfg).login("2468", "x")
    phonefix.write(cfg, {"lat": 40.86, "lon": -74.41, "accuracy_m": 9, "ts": 1}, "lan")
    notes.add(cfg, "solar_path_resistance", "Tightened the MPPT lugs", now=1)
    body = json.dumps({"data": {"/Devices/0/SerialNumber": "HQ1"}}).encode()
    victron_imports.add(cfg, "multi.vcsf",
                        vcsf.MAGIC + struct.pack(">I", len(body)) + zlib.compress(body), now=1)
    data = box / "data"
    data.mkdir()
    db = Db(str(data / "cerberus.db"))
    db.set_kv("device.id", "box-1")
    (data / "reports").mkdir()
    (data / "reports" / "2026-10-25.md").write_text("# drive\n")
    return cfg, data / "cerberus.db"


def test_bundle_has_what_the_new_box_needs(old_box, tmp_path):
    cfg, db = old_box
    out = tmp_path / "bundle"
    m = migrate.bundle(cfg, db, out)
    names = {p.name for p in out.iterdir()}
    assert {"config.yaml", "setup.yaml", ".env", ".cerberus-ui-pin.json",
            ".cerberus-phone-fix.json", ".cerberus-notes.json", "cerberus.db", "reports",
            ".cerberus-victron", "MANIFEST.json"} <= names
    assert auth.SESSIONS_FILE not in names                  # new address, new sign-in
    assert (out.stat().st_mode & 0o777) == 0o700 and (out / ".env").stat().st_mode & 0o077 == 0
    assert (out / "config.yaml").read_bytes() == cfg.read_bytes()
    assert Db(str(out / "cerberus.db")).get_kv("device.id") == "box-1"   # same box identity
    assert "reports/" in m["files"]
    assert len(victron_imports.read(out / "config.yaml")) == 1  # imports travel too


def test_new_box_settings_keep_the_ui_ones_and_clear_bluetooth_and_ha(old_box, tmp_path):
    cfg, db = old_box
    out = tmp_path / "bundle"
    migrate.bundle(cfg, db, out)
    loaded = cfg_mod.from_raw(cfg_mod.read_raw(out / "config.yaml"), password="x")
    assert loaded.weather.zip_code == "07054"                # the owner's settings survive
    assert dict(loaded.ble.pack_addresses) == {} and dict(loaded.ble.cabin_sensors) == {}
    assert loaded.ble.acr_address == "" and not loaded.ble.enabled       # until a scan
    assert loaded.ha_mqtt.enabled is False
    assert loaded.mqtt.portal_id == "0123456789ab"


def test_refuses_a_non_empty_output(old_box, tmp_path):
    cfg, db = old_box
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "x").write_text("")
    with pytest.raises(SystemExit):
        migrate.bundle(cfg, db, out)


def _compare_module():
    spec = importlib.util.spec_from_file_location("compare_boxes", ROOT / "tools/compare_boxes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now(soc=95.0, v=13.31, a=-4.0, pv=120.0, up=True):
    return {"collector_up": up, "cerbo_online": True,
            "snapshot": {"bank": {"soc": soc, "voltage": v, "current": a},
                         "solar": {"pv_w": pv}, "ac": None}}


def test_boxes_that_agree():
    c = _compare_module()
    assert all(ok for *_, ok in c.compare(_now(), _now(v=13.33, a=-5.5, pv=126)))


def test_boxes_that_disagree():
    c = _compare_module()
    bad = {label for label, _, _, ok in c.compare(_now(), _now(soc=80.0, up=False)) if not ok}
    assert bad == {"bank SoC %", "collector_up"}


def test_scripts_parse():
    import subprocess
    for s in ("bin/migrate-to-box.sh", "bin/install-bundle.sh", "bin/install-linux.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / s)]).returncode == 0


def test_install_bundle_installs_every_bundled_file(old_box, tmp_path):
    # install-bundle.sh names each file it installs; one the bundle carries
    # but the script skips is lost on the new box (the notes were, until
    # 2026-11-01).
    cfg, db = old_box
    m = migrate.bundle(cfg, db, tmp_path / "bundle")
    script = (ROOT / "bin/install-bundle.sh").read_text()
    missing = [f for f in m["files"] if f.rstrip("/") not in script]
    assert missing == []
