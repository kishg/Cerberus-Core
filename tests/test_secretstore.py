"""The secrets store (cerberus.secretstore, Core #51).

- set, get, delete, names; one non-empty line per value; safe names only.
- Modes: folder 0700, files 0600, and a loosened file is tightened on read.
- Atomic writes: a failed write leaves the old value and no stray file.
- Values never show: not in a repr, not in the command line's output.
- Cloud tokens: environment, then the store (own name, then cloud_token), then config.yaml.
- Migration carries the store, its modes, and storage mode; names only in the manifest.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import migrate, storage
from cerberus.db import Db
from cerberus.secretstore import (CLOUD_TOKEN, HEADER, SecretError, SecretStore, lookup, main)

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "iots_made-up-test-token-0000"


def mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def store(tmp_path):
    return SecretStore.for_config(tmp_path / "config.yaml")


def test_set_get_delete(store):
    assert store.get(CLOUD_TOKEN) is None and store.names() == []
    store.set(CLOUD_TOKEN, TOKEN)
    assert store.get(CLOUD_TOKEN) == TOKEN
    store.set(CLOUD_TOKEN, TOKEN + "-rotated")                 # replaced in place
    assert store.get(CLOUD_TOKEN) == TOKEN + "-rotated"
    assert store.names() == [CLOUD_TOKEN]
    assert store.delete(CLOUD_TOKEN) and not store.delete(CLOUD_TOKEN)
    assert store.get(CLOUD_TOKEN) is None


@pytest.mark.parametrize("name", ["../etc/passwd", "Cloud", "", ".hidden", "a/b", "x" * 70])
def test_bad_names(store, name):
    with pytest.raises(SecretError):
        store.set(name, "v")


@pytest.mark.parametrize("value", ["", "two\nlines", "cr\r", None])
def test_bad_values(store, value):
    with pytest.raises(SecretError):
        store.set(CLOUD_TOKEN, value)


def test_modes(store):
    store.set(CLOUD_TOKEN, TOKEN)
    assert mode(store.dir) == 0o700 and mode(store.dir / CLOUD_TOKEN) == 0o600
    os.chmod(store.dir / CLOUD_TOKEN, 0o644)                     # someone loosened it
    assert store.get(CLOUD_TOKEN) == TOKEN
    assert mode(store.dir / CLOUD_TOKEN) == 0o600


def test_format_line(store):
    store.set(CLOUD_TOKEN, TOKEN)
    assert (store.dir / CLOUD_TOKEN).read_text() == HEADER + TOKEN
    (store.dir / "sealed").write_text("cerberus-secret v2 tpm\nblob")
    os.chmod(store.dir / "sealed", 0o600)
    with pytest.raises(SecretError, match="can't read"):
        store.get("sealed")


def test_a_failed_write_keeps_the_old_value(store):
    store.set(CLOUD_TOKEN, TOKEN)
    with patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            store.set(CLOUD_TOKEN, "new")
    assert store.get(CLOUD_TOKEN) == TOKEN
    assert [p.name for p in store.dir.iterdir()] == [CLOUD_TOKEN]   # no stray temp file


def test_values_never_show(store, capsys, monkeypatch, tmp_path):
    store.set(CLOUD_TOKEN, TOKEN)
    assert TOKEN not in repr(store)
    cfg = str(tmp_path / "config.yaml")
    monkeypatch.setattr("sys.stdin", io.StringIO("another-made-up-secret\n"))
    assert main(["--config", cfg, "set", "sync_token"]) == 0
    assert main(["--config", cfg, "list"]) == 0
    assert main(["--config", cfg, "delete", "nope"]) == 0
    out = capsys.readouterr().out
    assert "another-made-up-secret" not in out and TOKEN not in out
    assert "sync_token" in out and CLOUD_TOKEN in out
    assert SecretStore.for_config(cfg).get("sync_token") == "another-made-up-secret"
    assert main(["--config", cfg, "set", "Bad Name"]) == 2


def test_lookup_order(store, monkeypatch):
    monkeypatch.delenv("X_TOKEN", raising=False)
    assert lookup("X_TOKEN", store, ("own", CLOUD_TOKEN), "from-config") == "from-config"
    store.set(CLOUD_TOKEN, "shared")
    assert lookup("X_TOKEN", store, ("own", CLOUD_TOKEN), "from-config") == "shared"
    store.set("own", "own-token")
    assert lookup("X_TOKEN", store, ("own", CLOUD_TOKEN), "from-config") == "own-token"
    monkeypatch.setenv("X_TOKEN", "from-env")
    assert lookup("X_TOKEN", store, ("own", CLOUD_TOKEN), "from-config") == "from-env"


def test_config_reads_cloud_tokens_from_the_store(tmp_path, monkeypatch):
    for v in ("CERBERUS_HEALTH_TOKEN", "CERBERUS_SYNC_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    cfgp = tmp_path / "config.yaml"
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["sync"]["auth_token"] = "from-config"
    cfgp.write_text(yaml.safe_dump(raw))
    assert cfg_mod.load(cfgp).sync.auth_token == "from-config"
    SecretStore.for_config(cfgp).set(CLOUD_TOKEN, TOKEN)
    c = cfg_mod.load(cfgp)
    assert c.sync.auth_token == TOKEN and c.health_report.auth_token == TOKEN


def test_migration_carries_the_store_and_storage_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    box = tmp_path / "old"
    box.mkdir()
    cfgp = box / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfgp)
    SecretStore.for_config(cfgp).set(CLOUD_TOKEN, TOKEN)
    storage.start(cfgp, 1_800_000_000)
    db = Db(str(box / "c.db"))
    db.close()
    m = migrate.bundle(cfgp, box / "c.db", tmp_path / "bundle")
    out = tmp_path / "bundle"
    assert mode(out / "secrets") == 0o700 and mode(out / "secrets" / CLOUD_TOKEN) == 0o600
    assert SecretStore(out / "secrets").get(CLOUD_TOKEN) == TOKEN
    assert (out / storage.STORAGE_FILE).exists()
    manifest = (out / "MANIFEST.json").read_text()
    assert CLOUD_TOKEN in manifest and TOKEN not in manifest
    script = (ROOT / "bin/install-bundle.sh").read_text()
    assert all(f.rstrip("/") in script for f in m["files"])
