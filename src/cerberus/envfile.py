"""The box's secrets file: `.env` beside config.yaml.

The Cerbo password (and the Govee and VRM keys) live here and nowhere else:
never in config.yaml, which is meant to be readable and shareable. bin/
run-collector.sh sources it as shell, and systemd reads it as an
EnvironmentFile, so values are written in the one form both accept: single
quoted, which rules out a single quote or a newline inside a value.

First-run setup writes the Cerbo password here (set_value), keeping every
other line as it was.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ENV_FILE = ".env"
MQTT_PASSWORD = "CERBERUS_MQTT_PASSWORD"


class EnvFileError(ValueError):
    pass


def path_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(ENV_FILE)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read(path: str | Path) -> dict[str, str]:
    """KEY=value lines; comments, blanks and `export ` prefixes allowed."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        out[key.strip()] = _unquote(value)
    return out


def check_value(value: str) -> str:
    if not value:
        raise EnvFileError("the value is empty")
    if "'" in value or "\n" in value or "\r" in value:
        raise EnvFileError("a single quote or a line break can't be stored in .env")
    return value


def set_value(path: str | Path, key: str, value: str) -> None:
    """Set one key, keeping every other line; owner-only, replaced atomically."""
    check_value(value)
    path = Path(path)
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    new = f"{key}='{value}'"
    for i, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith("export "):
            bare = bare[len("export "):]
        if bare.partition("=")[0].strip() == key and "=" in bare:
            lines[i] = new
            break
    else:
        lines.append(new)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def mqtt_password(config_path: str | Path) -> str:
    """The Cerbo password: the environment, else .env beside the config.

    The file is read too because the collector re-executes itself into new
    settings with its old environment, and a password set in the UI must
    still reach it (see Cerberus._config_changed)."""
    return os.environ.get(MQTT_PASSWORD) or read(path_for(config_path)).get(MQTT_PASSWORD, "")
