"""The box's secrets store: tokens and keys a running Cerberus can read and write (Core #51).

``.env`` (cerberus.envfile) holds the static secrets the services start with,
read once at start. Cloud features need more: tokens issued at pairing,
refreshed while running, and rotated without a restart. They live here:

- One file per secret in ``secrets/`` beside config.yaml (``/etc/cerberus/
  secrets/`` on a box). The folder is 0700 and each file 0600, owned by the
  user Cerberus runs as.
- Writes are atomic (temporary file, fsync, rename), so a crash or a power cut
  never leaves half a token.
- A value never appears in a log line, a repr, an exception or the command
  line's output. ``python -m cerberus.secretstore`` sets one from stdin and lists
  names only.
- Each file starts with a format line (``cerberus-secret v1 plain``), so a
  sealed format (TPM, e.g. systemd-creds) can be added later and old files
  still read. Today secrets are plain files protected by ownership and mode.

Lookup for the Cloud tokens (``lookup``): the environment first, then this
store, then config.yaml, so existing setups keep working.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

SECRETS_DIR = "secrets"
HEADER = "cerberus-secret v1 plain\n"
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

# Well-known names.
CLOUD_TOKEN = "cloud_token"            # the site token for the control plane (health and sync)
HEALTH_TOKEN = "health_report_token"   # a separate token for health reports, if one is used
SYNC_TOKEN = "sync_token"              # a separate token for sync, if one is used


class SecretError(ValueError):
    pass


class SecretStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    @classmethod
    def for_config(cls, config_path: str | Path) -> "SecretStore":
        return cls(Path(config_path).with_name(SECRETS_DIR))

    def __repr__(self) -> str:                      # names only, never values
        return f"SecretStore({str(self.dir)!r}, names={self.names()})"

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise SecretError("a secret's name is lowercase letters, digits, '.', '_' or '-'")
        return self.dir / name

    def get(self, name: str) -> str | None:
        p = self._path(name)
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                # Someone loosened it: tighten it back rather than trust the mode.
                os.chmod(p, 0o600)
                log.warning("secret %s was readable by others; tightened to 0600", name)
            text = p.read_text()
        except FileNotFoundError:
            return None
        if not text.startswith(HEADER):
            raise SecretError(f"secret {name} is in a format this Cerberus can't read")
        return text[len(HEADER):]

    def set(self, name: str, value: str) -> None:
        p = self._path(name)
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise SecretError("a secret's value is one non-empty line")
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".secret")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(HEADER + value)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("secret %s set", name)

    def delete(self, name: str) -> bool:
        p = self._path(name)
        try:
            p.unlink()
        except FileNotFoundError:
            return False
        log.info("secret %s deleted", name)
        return True

    def names(self) -> list[str]:
        try:
            return sorted(p.name for p in self.dir.iterdir()
                          if p.is_file() and NAME.fullmatch(p.name))
        except FileNotFoundError:
            return []


def lookup(env_var: str, store: SecretStore | None, names: tuple[str, ...], fallback: str = "") -> str:
    """The environment first, then the store (first name found), then the fallback (config.yaml)."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    if store is not None:
        for name in names:
            try:
                value = store.get(name)
            except SecretError as err:
                log.error("%s", err)
                value = None
            if value:
                return value
    return fallback


def main(argv: list[str] | None = None) -> int:
    """python -m cerberus.secretstore --config /etc/cerberus/config.yaml set cloud_token < token.txt"""
    ap = argparse.ArgumentParser(description="The box's secrets store. Values are never printed.")
    ap.add_argument("--config", default="config.yaml", help="config.yaml beside the store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="set a secret, its value read from stdin")
    s.add_argument("name")
    sub.add_parser("list", help="the names of the secrets that are set")
    d = sub.add_parser("delete", help="remove a secret")
    d.add_argument("name")
    args = ap.parse_args(argv)
    store = SecretStore.for_config(args.config)
    try:
        if args.cmd == "set":
            value = sys.stdin.readline().strip()
            store.set(args.name, value)
            print(f"{args.name} set")
        elif args.cmd == "list":
            for name in store.names():
                print(name)
        else:
            print(f"{args.name} {'deleted' if store.delete(args.name) else 'was not set'}")
    except SecretError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
