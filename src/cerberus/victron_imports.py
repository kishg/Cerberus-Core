"""VictronConnect settings files the owner imports, and the report on them.

The owner saves a device's settings in VictronConnect (a .vcsf) and uploads
it in Settings. Each file is kept as uploaded, so the folder doubles as a
settings backup, and the report says what changed since the previous file of
the same device and whether the settings the Cerbo also publishes still match
it (cerberus.device_settings). Diagnosing the settings is Co-Pilot's job, not
this module's.

Written by the UI, so it lives beside config.yaml, not in the database (the UI
never writes the database). Like the other beside-config state it is per-box,
git-ignored, and carried over by cerberus.migrate.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from . import vcsf

IMPORTS_DIR = ".cerberus-victron"
INDEX = "index.json"
# Real files are 3-4 KB; this keeps the base64 upload inside the UI's 64 KB body.
MAX_FILE = 45 * 1024
MAX_IMPORTS = 200
NAME_MAX = 120

_lock = threading.Lock()


class VictronImportError(ValueError):
    pass


def dir_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(IMPORTS_DIR)


def read(config_path: str | Path) -> list[dict]:
    """Every import, oldest first. A missing or damaged index is none."""
    try:
        raw = json.loads((dir_for(config_path) / INDEX).read_text())
    except (FileNotFoundError, ValueError):
        return []
    out = []
    for e in raw.get("imports", []) if isinstance(raw, dict) else []:
        try:
            out.append({"id": str(e["id"]), "ts": int(e["ts"]), "name": str(e["name"]),
                        "file": str(e["file"]), "device": str(e["device"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda e: e["ts"])


def _write_index(d: Path, entries: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".index-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"imports": entries}, f, indent=1)
        os.replace(tmp, d / INDEX)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(config_path: str | Path, name, raw: bytes, now: int | None = None) -> dict:
    """Validate and keep one uploaded file. Raises VictronImportError for anything
    that isn't a readable .vcsf."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise VictronImportError("choose a .vcsf file to import")
    if len(raw) > MAX_FILE:
        raise VictronImportError("that file is too large to be a VictronConnect settings file")
    try:
        f = vcsf.decode(bytes(raw))
    except vcsf.VcsfError as err:
        raise VictronImportError(str(err)) from None
    name = re.sub(r"[^\w.\- ]", "_", str(name or "settings.vcsf"))[:NAME_MAX]
    ts = int(time.time()) if now is None else now
    d = dir_for(config_path)
    with _lock:
        entries = read(config_path)
        if len(entries) >= MAX_IMPORTS:
            raise VictronImportError(f"{MAX_IMPORTS} files are already imported; delete old ones first")
        d.mkdir(mode=0o700, exist_ok=True)
        eid = secrets.token_hex(6)
        stored = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(ts))}-{eid}.vcsf"
        p = d / stored
        p.write_bytes(bytes(raw))
        os.chmod(p, 0o600)
        entry = {"id": eid, "ts": ts, "name": name, "file": stored, "device": f.device_key}
        _write_index(d, entries + [entry])
    return entry


def delete(config_path: str | Path, import_id) -> bool:
    d = dir_for(config_path)
    with _lock:
        entries = read(config_path)
        keep = [e for e in entries if e["id"] != str(import_id)]
        if len(keep) == len(entries):
            return False
        gone = next(e for e in entries if e["id"] == str(import_id))
        _write_index(d, keep)
        try:
            (d / gone["file"]).unlink()
        except OSError:
            pass
    return True


def load(config_path: str | Path, entry: dict) -> vcsf.Vcsf | None:
    try:
        return vcsf.decode((dir_for(config_path) / Path(entry["file"]).name).read_bytes())
    except (OSError, vcsf.VcsfError):
        return None


# ---------------------------------------------------------------- report

def live_check(conn: sqlite3.Connection | None, f: vcsf.Vcsf) -> list[dict]:
    """The file's settings that the Cerbo also publishes, against the value
    Cerberus last recorded (cerberus.device_settings). A mismatch means the
    device was changed after the file was saved, or the file isn't this
    van's."""
    if conn is None or f.kind not in ("vebus", "solarcharger"):
        return []
    try:
        rows = conn.execute(
            "SELECT path, value, max(ts) FROM device_settings WHERE path LIKE ? GROUP BY path",
            (f"{f.kind}/%",)).fetchall()
    except sqlite3.Error:
        return []
    live: dict[str, object] = {}
    for path, value, _ts in rows:
        rest = "/" + path.split("/", 2)[2]          # vebus/276/Devices/0/... -> /Devices/0/...
        try:
            live.setdefault(rest, json.loads(value))
        except ValueError:
            continue
    out = []
    for path, saved in sorted(f.settings().items()):
        if path not in live or saved is None:
            continue
        name, unit = vcsf.label(path)
        out.append({"path": path, "name": name, "unit": unit, "file": saved, "live": live[path],
                    "file_text": vcsf.fmt(path, saved), "live_text": vcsf.fmt(path, live[path]),
                    "match": vcsf.same(_as_number(saved), _as_number(live[path]))})
    return out


def _as_number(v):
    """The file saves a switch as 1/0 and MQTT sends it as true/false; across
    the two sources they mean the same."""
    return int(v) if isinstance(v, bool) else v


def _summary(f: vcsf.Vcsf) -> dict:
    return {"kind": f.kind, "serial": f.serial, "product_id": f.product_id,
            "firmware": f.firmware, "app": f"{f.meta.get('appName', '')} "
                                           f"{f.meta.get('appversion', '')}".strip(),
            "description": f.meta.get("description") or ""}


def view(config_path: str | Path, conn: sqlite3.Connection | None = None) -> dict:
    """Per device, newest import first: what changed since that device's
    previous import, and for the newest, the check against the Cerbo."""
    devices: dict[str, list] = {}
    for e in read(config_path):
        devices.setdefault(e["device"], []).append(e)
    out = []
    for key, entries in devices.items():
        files, prev = [], None
        for e in entries:                            # oldest first
            f = load(config_path, e)
            item = {"id": e["id"], "ts": e["ts"], "name": e["name"], "readable": f is not None}
            if f is not None:
                item.update(_summary(f), settings_count=len(f.settings()))
                item["changes"] = vcsf.diff(prev.settings(), f.settings()) if prev else None
                prev = f
            files.append(item)
        newest = load(config_path, entries[-1])
        out.append({"device": key, "kind": newest.kind if newest else None,
                    "imports": list(reversed(files)),
                    "live_check": live_check(conn, newest) if newest else [],
                    "settings": [{"path": p, "name": vcsf.label(p)[0], "text": vcsf.fmt(p, v)}
                                 for p, v in sorted(newest.settings().items())]
                    if newest else []})
    out.sort(key=lambda d: -d["imports"][0]["ts"])
    return {"devices": out}
