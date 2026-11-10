"""Moving a running Cerberus to a new box (step 5.0: this van, Mac -> mini PC).

    python -m cerberus.migrate bundle --config config.yaml --db soak/data/cerberus.db --out DIR

writes DIR (owner-only), everything the new box needs:

* config.yaml as is (its comments are the record of this rig);
* setup.yaml: the UI's settings, plus what must change on the new box:
  - **Bluetooth addresses cleared, BLE off.** macOS gives each device a
    per-host UUID; Linux uses its MAC, so the old ones mean nothing there.
    The sidecar waits until Settings > Find Bluetooth devices > Use selected
    on the new box turns BLE back on with the right addresses.
  - **Home Assistant off.** HA runs on the old box (127.0.0.1 there); on by
    default it would publish into nothing. Turn it on in Settings with the
    broker's address if HA moves or stays reachable.
* .env (Cerbo password, Govee and VRM keys), the PIN hash, the last phone
  position. Not sessions: the box has a new address, and browsers keep a
  sign-in per address, so everyone signs in again there anyway.
* cerberus.db: a consistent online copy (SQLite's backup API), taken while
  the collector keeps running, plus the drive reports.

bin/migrate-to-box.sh copies the bundle to the box and installs it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import yaml

from . import config as cfg_mod
from . import envfile
from . import notes, phonefix, victron_imports
from . import settings as settings_mod
from .ui import auth

# What the new box overrides in setup.yaml, and why (also in MANIFEST).
OVERRIDES = {
    "ble": {"enabled": False, "pack_addresses": {}, "cabin_sensors": {}, "acr_address": ""},
    "ha_mqtt": {"enabled": False},
}
NOTES = [
    "Bluetooth addresses cleared: macOS UUIDs mean nothing on Linux. Run "
    "Settings > Find Bluetooth devices on the new box.",
    "Home Assistant publishing off: HA's broker was on the old box. Turn it on "
    "in Settings with the broker's address if HA stays reachable.",
    "Sign in again on the new box's address (a browser keeps sign-ins per address).",
]


def target_setup(config_path: Path) -> dict:
    """The old box's setup.yaml with the new box's overrides, validated."""
    current = settings_mod._read_yaml(cfg_mod.setup_path(config_path))
    merged = cfg_mod.merge(current, OVERRIDES)
    base = yaml.safe_load(Path(config_path).read_text()) or {}
    cfg_mod.from_raw(cfg_mod.merge(base, merged), password="validate-only")
    return merged


def backup_db(src: Path, dst: Path) -> int:
    """Consistent copy of a live database; returns its size in bytes."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst.stat().st_size


def bundle(config_path: Path, db_path: Path, out: Path) -> dict:
    config_path, db_path = Path(config_path), Path(db_path)
    if not config_path.exists():
        raise SystemExit(f"no {config_path}")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    files = {}

    shutil.copyfile(config_path, out / "config.yaml")
    files["config.yaml"] = "as is"
    setup = target_setup(config_path)
    (out / cfg_mod.SETUP_FILE).write_text(
        "# Written by cerberus.migrate for the new box; values here override\n"
        "# config.yaml. Bluetooth cleared and HA off: see MANIFEST.json.\n"
        + yaml.safe_dump(setup, sort_keys=True, default_flow_style=False))
    files[cfg_mod.SETUP_FILE] = "UI settings + new-box overrides"

    for name in (envfile.ENV_FILE, auth.PIN_FILE, phonefix.PHONE_FILE, notes.NOTES_FILE):
        src = config_path.with_name(name)
        if src.exists():
            shutil.copyfile(src, out / name)
            os.chmod(out / name, 0o600)
            files[name] = "copied"

    imports = config_path.with_name(victron_imports.IMPORTS_DIR)
    if imports.is_dir():
        shutil.copytree(imports, out / victron_imports.IMPORTS_DIR)
        files[victron_imports.IMPORTS_DIR + "/"] = (
            f"{len(victron_imports.read(config_path))} imported settings files")

    size = backup_db(db_path, out / "cerberus.db")
    files["cerberus.db"] = f"{size / 1e6:.1f} MB, online backup"
    reports = db_path.parent / "reports"
    if reports.is_dir():
        shutil.copytree(reports, out / "reports")
        files["reports/"] = f"{sum(1 for _ in (out / 'reports').iterdir())} drive reports"

    manifest = {"created": int(time.time()), "files": files, "overrides": OVERRIDES,
                "notes": NOTES}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(prog="cerberus.migrate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle", help="collect everything a new box needs")
    b.add_argument("--config", type=Path, default=Path("config.yaml"))
    b.add_argument("--db", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    m = bundle(args.config, args.db, args.out)
    for name, what in m["files"].items():
        print(f"  {name}: {what}")
    for n in m["notes"]:
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
