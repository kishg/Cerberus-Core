"""A trusted local client: a program on the box itself that may change a few settings without the PIN.

Off unless configured. With ``local_client.token_file`` set, a request to the box's own UI server
that comes from the box itself (a loopback address, not proxied: ``phonefix.client_kind`` says BOX)
and carries that file's token as ``Authorization: Bearer ...`` may:

- link the box to a cloud service: store its token in the secret store and turn health reports and
  sync on or off, pointing at HTTPS addresses (``POST /api/v1/cloud/link``), or unlink it
  (``POST /api/v1/cloud/unlink``);
- turn storage mode on or off (``POST /api/v1/storage``), as the owner's PIN session can.

Nothing else: every other change still needs the owner's PIN. The token file is made by whoever
installs that client (for example a subscription service's agent) and must be readable by the
Cerberus user and that client only; a file anyone else can read is ignored. Every action is logged.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import config as cfg_mod
from . import secretstore, settings as settings_mod
from . import phonefix

log = logging.getLogger(__name__)

SYNC_KEYS = ("enabled", "target_url", "tables", "share_position", "metered")
HEALTH_KEYS = ("enabled", "target_url", "interval_seconds")


class LocalClientError(ValueError):
    """The request is from the local client but asks for something it may not do."""


def _raw(config_path: str | Path) -> dict:
    base = yaml.safe_load(Path(config_path).read_text()) or {}
    sp = cfg_mod.setup_path(config_path)
    over = (yaml.safe_load(sp.read_text()) if sp.exists() else None) or {}
    return cfg_mod.merge(base, over)


def token_file(config_path: str | Path | None) -> Path | None:
    if not config_path:
        return None
    try:
        path = ((_raw(config_path).get("local_client") or {}).get("token_file"))
    except (OSError, yaml.YAMLError):
        return None
    return Path(path) if path else None


def _token(config_path) -> str | None:
    path = token_file(config_path)
    if not path:
        return None
    try:
        st = path.stat()
        if st.st_mode & 0o007:                          # readable by anyone: not a secret
            log.warning("local client token %s is readable by others; ignoring it", path)
            return None
        value = path.read_text().strip()
    except OSError:
        return None
    return value if len(value) >= 32 else None


def authorized(config_path, addr: str, headers) -> bool:
    """Whether this request is the configured local client, on the box itself, with its token."""
    expected = _token(config_path)
    if not expected:
        return False
    if phonefix.client_kind(addr, headers) != phonefix.BOX:
        return False
    auth = headers.get("Authorization", "")
    given = auth[7:].strip() if auth.startswith("Bearer ") else ""
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def _https(url, what: str) -> str:
    u = urlsplit(str(url or ""))
    if u.scheme != "https" or not u.hostname:
        raise LocalClientError(f"{what}: an https:// address is needed")
    return str(url)


TYPES = {"enabled": bool, "share_position": bool, "metered": bool, "interval_seconds": int,
         "target_url": str, "tables": (str, list)}


def link(config_path: str | Path, body: dict) -> dict:
    """Store the cloud token and set health reports and sync. Returns what is now set.

    What a local client may not do without the owner (security review S12): point health reports or
    sync at a different service than the one the box is linked to (unlinking keeps the address, so
    a new service needs the owner), or turn on sharing the van's position."""
    token = body.get("cloud_token")
    if not isinstance(token, str) or not 16 <= len(token) <= 512 or not token.isprintable():
        raise LocalClientError("cloud_token: expected the service's token")
    current = _raw(config_path)
    changes: dict = {}
    for section, keys in (("health_report", HEALTH_KEYS), ("sync", SYNC_KEYS)):
        given = body.get(section)
        if given is None:
            continue
        if not isinstance(given, dict) or set(given) - set(keys):
            raise LocalClientError(f"{section}: only {', '.join(keys)}")
        for k, v in given.items():
            if not isinstance(v, TYPES[k]) or (TYPES[k] is int and isinstance(v, bool)):
                raise LocalClientError(f"{section}.{k}: wrong type")
        now = current.get(section) or {}
        now = now if isinstance(now, dict) else {}
        if "target_url" in given:
            _https(given["target_url"], f"{section}.target_url")
            was = urlsplit(str(now.get("target_url") or "")).hostname
            if was and was != urlsplit(given["target_url"]).hostname:
                raise LocalClientError(f"{section} goes to {was}; sending it elsewhere needs the owner at the box")
        if given.get("share_position") is True and now.get("share_position") is not True:
            raise LocalClientError("sharing the van's position needs the owner's PIN at the box")
        changes[section] = dict(given)
    setup = _prepare(config_path, changes)             # checked to load before anything is stored
    secretstore.SecretStore.for_config(config_path).set("cloud_token", token)
    if setup is not None:
        settings_mod.write_setup(config_path, setup)
    log.info("local client linked the box to %s", ", ".join(
        sorted({urlsplit(v.get("target_url", "")).hostname or "?" for v in changes.values()})) or "a service")
    return {"linked": True, "set": {k: sorted(v) for k, v in changes.items()}}


def unlink(config_path: str | Path) -> dict:
    """Forget the cloud token and turn health reports and sync off."""
    _write(config_path, {"health_report": {"enabled": False}, "sync": {"enabled": False}})
    secretstore.SecretStore.for_config(config_path).delete("cloud_token")
    log.info("local client unlinked the box")
    return {"linked": False}


def _prepare(config_path, changes: dict) -> dict | None:
    """setup.yaml with `changes` merged in, refused if it wouldn't load; None when nothing changes."""
    if not changes:
        return None
    sp = cfg_mod.setup_path(config_path)
    setup = (yaml.safe_load(sp.read_text()) if sp.exists() else None) or {}
    for section, values in changes.items():
        if not isinstance(setup.get(section), dict):   # absent, or left null by hand
            setup[section] = {}
        setup[section].update(values)
    base = yaml.safe_load(Path(config_path).read_text()) or {}
    try:
        cfg_mod.from_raw(cfg_mod.merge(base, setup), password=os.environ.get("CERBERUS_MQTT_PASSWORD") or "validate-only")
    except cfg_mod.ConfigError as err:
        raise LocalClientError(f"that wouldn't load: {err}") from None
    return setup


def _write(config_path, changes: dict) -> None:
    setup = _prepare(config_path, changes)
    if setup is not None:
        settings_mod.write_setup(config_path, setup)
