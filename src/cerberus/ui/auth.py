"""The settings PIN: viewing the UI is open on the LAN, changing settings is not.

The PIN is stored only as a salted PBKDF2 hash, in a file beside config.yaml
readable by its owner alone (never in config.yaml, which may be shared or
committed). It can be set only from the box itself: `python -m cerberus.ui
--set-pin`, or the web UI opened on the box (a loopback address). Anyone on
the van's network can look; only someone at the box can claim the PIN.

A correct PIN returns a session token. Sessions survive UI restarts: they
are kept in a file beside the PIN's (owner-only), as SHA-256 hashes of the
tokens, never the tokens themselves, so the file can't be used to sign in.
A session lasts SESSION_S from its last use (renewed at most once per
RENEW_S, so the file isn't rewritten on every request): a phone that keeps
sharing its location stays signed in. Signing out ends one session; setting
a new PIN ends them all. Repeated wrong PINs lock out for LOCKOUT_S, per
address (in memory).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

PIN_FILE = ".cerberus-ui-pin.json"
SESSIONS_FILE = ".cerberus-ui-sessions.json"
ITERATIONS = 200_000
SESSION_S = 30 * 86400
RENEW_S = 86400
MAX_FAILURES = 5
LOCKOUT_S = 60
MIN_LEN, MAX_LEN = 4, 12


class AuthError(Exception):
    pass


def pin_path(config_path: str | Path) -> Path:
    return Path(config_path).with_name(PIN_FILE)


def sessions_path(config_path: str | Path) -> Path:
    return Path(config_path).with_name(SESSIONS_FILE)


def _write_private(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _hash(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)


def validate_pin(pin: str) -> str:
    pin = str(pin or "").strip()
    if not pin.isdigit() or not MIN_LEN <= len(pin) <= MAX_LEN:
        raise AuthError(f"PIN must be {MIN_LEN}-{MAX_LEN} digits")
    return pin


def set_pin(config_path: str | Path, pin: str) -> None:
    pin = validate_pin(pin)
    salt = secrets.token_bytes(16)
    data = {"salt": salt.hex(), "iterations": ITERATIONS,
            "hash": _hash(pin, salt, ITERATIONS).hex()}
    _write_private(pin_path(config_path), data)
    # A new PIN signs everyone out, including a UI that is running: it
    # notices the file change (Sessions._reload).
    sessions_path(config_path).unlink(missing_ok=True)


def has_pin(config_path: str | Path) -> bool:
    return pin_path(config_path).exists()


def check_pin(config_path: str | Path, pin: str) -> bool:
    p = pin_path(config_path)
    if not p.exists():
        return False
    data = json.loads(p.read_text())
    want = bytes.fromhex(data["hash"])
    got = _hash(str(pin or ""), bytes.fromhex(data["salt"]), int(data["iterations"]))
    return hmac.compare_digest(want, got)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Session tokens (persisted, hashed) and per-address lockout."""

    def __init__(self, config_path: str | Path, clock=time.time):
        self.config_path = config_path
        self.clock = clock
        self._tokens: dict[str, float] = {}       # sha256(token) -> expiry
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._mtime: int | None = -1
        with self._lock:
            self._reload()

    def _reload(self) -> None:
        """Read the file if it changed underneath us (a new PIN deletes it)."""
        p = sessions_path(self.config_path)
        try:
            mtime = p.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self._tokens = {}
        if mtime is None:
            return
        try:
            data = json.loads(p.read_text())
            self._tokens = {str(k): float(v) for k, v in (data.get("sessions") or {}).items()}
        except (ValueError, AttributeError, TypeError):
            self._tokens = {}

    def _save(self) -> None:
        now = self.clock()
        self._tokens = {k: exp for k, exp in self._tokens.items() if exp > now}
        p = sessions_path(self.config_path)
        _write_private(p, {"v": 1, "sessions": self._tokens})
        self._mtime = p.stat().st_mtime_ns

    def login(self, pin: str, addr: str) -> str:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(addr, []) if now - t < LOCKOUT_S]
            self._fails[addr] = recent
            if len(recent) >= MAX_FAILURES:
                raise AuthError("Too many wrong PINs; wait a minute and try again")
        if not has_pin(self.config_path):
            raise AuthError("No PIN is set yet; set one on the box first")
        if not check_pin(self.config_path, pin):
            with self._lock:
                self._fails.setdefault(addr, []).append(now)
            raise AuthError("Wrong PIN")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._fails.pop(addr, None)
            self._reload()
            self._tokens[_digest(token)] = now + SESSION_S
            self._save()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = self.clock()
        with self._lock:
            self._reload()
            key = _digest(token)
            exp = self._tokens.get(key)
            if exp is None or exp <= now:
                return False
            if exp - now < SESSION_S - RENEW_S:      # renew, at most once a day
                self._tokens[key] = now + SESSION_S
                self._save()
            return True

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._reload()
            if self._tokens.pop(_digest(token or ""), None) is not None:
                self._save()


def is_loopback(addr: str) -> bool:
    return addr in ("127.0.0.1", "::1") or addr.startswith("127.")
