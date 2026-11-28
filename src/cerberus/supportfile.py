"""The support file: one download the owner emails to support (Core #44).

Settings > Support > Download support file (signed in) gives one zip of
plain-text files: what the box knows about itself and the van, for when
support can't reach the box (the van is offline, access isn't granted, sync
is off). Everything comes from what is stored, so it works with the Cerbo
offline.

Inside (``SECTIONS``):

- ``status.md``    the status table, 24 h vs the 24 h before (cerberus.status,
                   as tools/status_report.py prints it)
- ``health.json``  sources, coverage, gaps and gap episodes (cerberus.health)
- ``findings.json`` findings now and every numbered occurrence, with the
                   owner's notes (cerberus.findings_log, cerberus.notes)
- ``events.json``  the recent events
- ``box.json``     Core version, device ID and box health (uptime, disk,
                   memory, CPU temperature, restarts, the GX link)
- ``config.yaml`` / ``setup.yaml``  the settings, with secrets removed
- ``logs/*.log``   the last lines of each service's own log file (cerberus.log), never
                   the system journal

**No secrets.** Never read into the file: ``.env``, the secrets folder, the
PIN and session files. Secrets are kept out three ways, each enough alone
for what it covers:

1. Structured data (config, setup.yaml, events, findings) goes through
   ``redact()``: a key whose name is secret-shaped (``secret_key``) has its
   value replaced by ``REMOVED``, unless the value is a boolean, a number or
   empty, which can't hold a secret.
2. Every file's text goes through ``scrub_text()``: ``Bearer`` tokens,
   ``password=...``-style pairs, credentials in URLs and long random-looking
   strings are replaced (``SECRET_PATTERNS``).
3. Every value the box actually holds as a secret (``known_secrets()``:
   every ``.env`` value, every secret in the store, the PIN hash and salt, the
   session hashes, the config's secret-named values, and secret-named
   variables in this process's environment) is replaced wherever it appears,
   in any form the patterns missed.

**No position** unless the owner ticks the box: lat/lon keys, the home ZIP,
the van's stored position and coordinate pairs in text are removed.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config as cfg_mod
from . import envfile, findings_log, health, healthreport, location, notes, status
from . import log as log_mod
from .secretstore import SecretStore

REMOVED = "[removed]"
POSITION_REMOVED = "[position removed]"
FORMAT = "cerberus.support"
VERSION = 1
LOG_LINES = 300
EVENTS_DAYS = 7
EVENTS_MAX = 1000
KV_DEVICE_ID = "device.id"

# Each service's log: its own file on a box (cerberus.log: /var/log/cerberus/<service>.log), never
# the system journal; on a Mac, the launchd logs beside config.yaml (soak/).
BOX_LOG_DIR = "/var/log/cerberus"
MAC_LOG_FILES = ("cerberus.log", "cerberus-ui.log", "cerberus-ble.log")
MAC_LOG_DIR = "soak"
AUTO = object()

SECTIONS = (
    ("status.md", "Status table: the last 24 h against the 24 h before"),
    ("health.json", "Data sources, coverage, gaps and their likely causes"),
    ("findings.json", "Findings now and every numbered occurrence, with your notes"),
    ("events.json", f"Events from the last {EVENTS_DAYS} days"),
    ("box.json", "Core version, device ID and box health (uptime, disk, memory, temperature)"),
    ("config.yaml", "Your configuration, with secrets removed"),
    ("setup.yaml", "Settings changed in this app, with secrets removed"),
    ("logs/", f"The last {LOG_LINES} lines of each service's log, with secrets removed"),
)
NEVER = (".env (the Cerbo password, API keys)", "the PIN and sign-in sessions",
         "tokens and keys (cloud, sync, health reports, VRM, Govee)", "Wi-Fi passwords")


# -- what counts as secret ----------------------------------------------------------

# A key name is split into words (snake, kebab, dotted, camelCase); it is secret
# if one word is in SECRET_WORDS, or two adjacent words make a SECRET_PAIRS entry.
SECRET_WORDS = frozenset({
    "password", "passwd", "pwd", "pass", "passphrase", "passcode", "pin", "token", "tokens",
    "secret", "secrets", "apikey", "auth", "authorization", "cookie", "cookies", "session",
    "sessions", "credential", "credentials", "psk", "bearer", "salt", "hash", "jwt", "otp",
})
SECRET_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"),
                          ("setup", "code"), ("pairing", "code"), ("app", "key"), ("client", "secret"),
                          ("x", "authorization"), ("wifi", "key")})
# Names that look secret-shaped but are not: kept.
ALLOWED_KEYS = frozenset({"pin_set", "token_set", "auth_required", "session_s"})

# Never carry a position unless the owner ticks the box.
POSITION_KEYS = frozenset({"lat", "lon", "lng", "latitude", "longitude", "position", "position_source",
                           "gps", "zip", "zip_code", "postcode", "coords", "coordinates"})


def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    return [w for w in re.split(r"[^A-Za-z0-9]+", name.lower()) if w]


def is_secret_key(name) -> bool:
    """Deny-list by words in the key's name, with an allow-list of known-safe names."""
    if not isinstance(name, str) or name.lower() in ALLOWED_KEYS:
        return False
    w = _words(name)
    return any(x in SECRET_WORDS for x in w) or any(p in SECRET_PAIRS for p in zip(w, w[1:]))


def _can_hold_secret(value) -> bool:
    return not (value is None or isinstance(value, (bool, int, float)) or value == "" or value == [] or value == {})


def redact(obj, include_position: bool = False):
    """A copy with secret-named values replaced and (unless included) position keys dropped."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not include_position and isinstance(k, str) and k.lower() in POSITION_KEYS:
                continue
            out[k] = REMOVED if is_secret_key(k) and _can_hold_secret(v) else redact(v, include_position)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, include_position) for v in obj]
    if isinstance(obj, str) and obj[:1] in "{[":
        try:                                       # JSON text inside a row (event detail)
            parsed = json.loads(obj)
        except ValueError:
            return obj
        return json.dumps(redact(parsed, include_position), sort_keys=True)
    if isinstance(obj, str) and not include_position:
        return _scrub_position(obj)                # coordinates typed into a note or an explanation
    return obj


_KEYWORDS = (r"password|passwd|passphrase|passcode|token|secret|api[_-]?key|apikey|authorization|"
             r"cookie|credentials?|psk|private[_-]?key|access[_-]?key|setup[_-]?code")
_SAFE_VALUE = r"(?!(?:true|false|null|none|yes|no|on|off|\[removed\])\b)"
SECRET_PATTERNS = (
    # Authorization: Bearer abc / Basic abc
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 " + REMOVED),
    # user:pass@ in a URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REMOVED + "@"),
    # key=value, key: value, "key": "value" for any secret-shaped key name (auth_token, x-authorization ...)
    (re.compile(r"(?i)(\b[\w.-]*(?:" + _KEYWORDS + r")[\w.-]*[\"']?\s*[:=]\s*[\"']?)" + _SAFE_VALUE
                + r"([^\s\"',;&}]+)"), r"\1" + REMOVED),
    # pin=1234, ?pin=..., "pin": "1234", auth=...
    (re.compile(r"(?i)(\b(?:pin|auth|session|sid)[\"']?\s*[:=]\s*[\"']?)" + _SAFE_VALUE + r"([^\s\"',;&}]+)"),
     r"\1" + REMOVED),
    # A long random-looking string (a token, a key, a hash): 32+ letters/digits with both in it.
    # UUIDs (the device ID, Bluetooth addresses) are kept: they're identifiers, not secrets.
    (re.compile(r"(?<![A-Za-z0-9_+=-])(?![0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{12}(?![A-Za-z0-9_+=-]))(?=[A-Za-z0-9_+=-]*[0-9])(?=[A-Za-z0-9_+=-]*[A-Za-z])"
                r"[A-Za-z0-9_+=-]{32,}"), REMOVED),
)
POSITION_PATTERNS = (
    # 39.7392, -104.9903 (a coordinate pair)
    (re.compile(r"-?\b\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}\b"), POSITION_REMOVED),
    # lat=39.7392, "longitude": -104.99
    (re.compile(r"(?i)(\b(?:lat|lon|lng|latitude|longitude)[\"']?\s*[:=]\s*)-?\d+(?:\.\d+)?"),
     r"\1" + POSITION_REMOVED),
)
MIN_SECRET_LEN = 4


def _scrub_position(text: str) -> str:
    for rx, repl in POSITION_PATTERNS:
        text = rx.sub(repl, text)
    return text
          # shorter values would scrub ordinary text; they're caught by name instead


def scrub_text(text: str, secrets: set[str] | frozenset = frozenset(), include_position: bool = False) -> str:
    """Remove every known secret value, then anything secret-shaped, then (unless included) positions.

    JSON files pass include_position=True here: their positions went in redact(), by key and inside
    strings, where a coordinate pattern can't break the JSON."""
    for s in sorted((s for s in secrets if s and len(s) >= MIN_SECRET_LEN), key=len, reverse=True):
        text = text.replace(s, REMOVED)
        # The same value as it reads inside JSON (escaped quotes, backslashes, unicode).
        esc = json.dumps(s)[1:-1]
        if esc != s:
            text = text.replace(esc, REMOVED)
    for rx, repl in SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text if include_position else _scrub_position(text)


def _secret_values(obj, out: set[str], under_secret: bool = False) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _secret_values(v, out, under_secret or is_secret_key(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _secret_values(v, out, under_secret)
    elif under_secret and isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        out.add(str(obj))


def _json_file(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def known_secrets(config_path: str | Path) -> set[str]:
    """Every secret value this box holds, read only so it can be removed from the file."""
    from .ui import auth                         # the UI's PIN and session files
    cp = Path(config_path)
    out: set[str] = set()
    out.update(v for v in envfile.read(envfile.path_for(cp)).values() if v)   # every .env value
    store = SecretStore.for_config(cp)
    try:
        for name in store.names():
            v = store.get(name)
            if v:
                out.add(v)
    except Exception:                            # an unreadable store: its values never get read in anyway
        pass
    for p in (auth.pin_path(cp), auth.sessions_path(cp)):
        data = _json_file(p)
        if data is not None:
            _secret_values(data, out, under_secret=True)
    try:
        _secret_values(cfg_mod.read_raw(cp), out)
    except Exception:
        pass
    for k, v in os.environ.items():
        if v and is_secret_key(k):
            out.add(v)
    return out


# -- the sections -------------------------------------------------------------------

def _connect_ro(db_path: str) -> sqlite3.Connection:
    from urllib.parse import quote
    conn = sqlite3.connect(f"file:{quote(str(Path(db_path).resolve()))}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _kv(conn, key):
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def events(conn: sqlite3.Connection, now: int, days: int = EVENTS_DAYS, limit: int = EVENTS_MAX) -> list[dict]:
    rows = conn.execute("SELECT ts, type, severity, detail FROM events WHERE ts >= ? "
                        "ORDER BY ts DESC LIMIT ?", (now - days * 86400, limit)).fetchall()
    out = []
    for ts, typ, sev, detail in rows:
        try:
            d = json.loads(detail) if detail else {}
        except ValueError:
            d = detail
        if isinstance(d, dict) and set(d) >= {"type", "detail"}:     # the whole event, as Db stores it
            d = d["detail"]
        out.append({"ts": ts, "type": typ, "severity": sev, "detail": d})
    return out


def box_health(conn: sqlite3.Connection | None, now: int, db_path: str) -> dict:
    """What the box is and how it is: the health report's fields, without sending anything."""
    out = {"core_version": healthreport.core_version(), "device_id": _kv(conn, KV_DEVICE_ID) if conn else None,
           "uptime_host_s": healthreport.host_uptime_s(),
           "system": {"disk": healthreport.disk(db_path), "memory": healthreport.memory(),
                      "cpu_temp_c": healthreport.cpu_temp_c()}}
    if conn is not None:
        try:
            out["collector_restarts_24h"] = healthreport.restarts_since(conn, now - 86400)
            out["collector_down_s_24h"] = healthreport.collector_down_s(conn, now - 86400, now)
            out["gx"] = healthreport.gx_link(conn, now)
        except sqlite3.Error as err:
            out["error"] = f"database: {err}"
    return out


def _tail_file(path: Path, n: int) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-n:]) + "\n"


def _tail_rotated(path: Path, n: int) -> str:
    """The last n lines of a service's log, topped up from its previous file (.1) just after a rotation."""
    text = _tail_file(path, n)
    lines = text.splitlines()
    older = path.with_name(path.name + ".1")
    if len(lines) < n and older.is_file():
        try:
            lines = _tail_file(older, n - len(lines)).splitlines() + lines
        except OSError:
            pass
    return "\n".join(lines) + "\n"


def log_dir() -> str | None:
    """Where the services' own log files are: CERBERUS_LOG_DIR (the units set it), else
    /var/log/cerberus on Linux; None on a Mac, whose logs are in soak/ beside config.yaml."""
    d = os.environ.get(log_mod.LOG_DIR_ENV)
    if d:
        return d
    return BOX_LOG_DIR if sys.platform.startswith("linux") else None


def service_logs(config_path: str | Path, n: int = LOG_LINES, directory: str | Path | None | object = AUTO
                 ) -> dict[str, str]:
    """{file name: the last n lines}; a line saying why when a log can't be read.

    On a box: each service's file in the log folder (cerberus.log), never the system journal.
    directory=None: a Mac's soak/*.log beside config.yaml (launchd writes them)."""
    if directory is AUTO:
        directory = log_dir()
    out: dict[str, str] = {}
    if directory is not None:
        paths = [(f"{svc}.log", log_mod.log_path(directory, svc)) for svc in log_mod.SERVICES]
    else:
        folder = Path(config_path).parent / MAC_LOG_DIR
        paths = [(name, folder / name) for name in MAC_LOG_FILES if (folder / name).is_file()]
    for name, p in paths:
        try:
            out[name] = _tail_rotated(p, n)
        except OSError as err:
            out[name] = f"(couldn't be read: {name}: {err.strerror or err})\n"
    return out


def _yaml_file(path: Path):
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as err:
        return {"error": f"does not load: {err}"}


def config_files(config_path: str | Path, include_position: bool = False) -> dict[str, str]:
    """config.yaml and setup.yaml, redacted, as YAML (comments are not carried)."""
    cp = Path(config_path)
    out = {}
    for name, p in (("config.yaml", cp), ("setup.yaml", cfg_mod.setup_path(cp))):
        data = _yaml_file(p)
        out[name] = ("# not present\n" if data is None else
                     yaml.safe_dump(redact(data, include_position), sort_keys=False, allow_unicode=True))
    return out


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n"


def build(config_path: str | Path, db_path: str, *, include_position: bool = False,
          now: int | None = None, log_dir: str | Path | None | object = AUTO,
          ctx=None) -> dict[str, str]:
    """Every file in the support file, {path: text}, secrets (and position, unless included) removed.

    ``ctx`` is the UI's api.Context (time zone, pack count, thresholds); without one, defaults."""
    from .ui import api
    now = int(time.time()) if now is None else now
    ctx = ctx or api.Context()
    owner_notes = notes.read(config_path)
    files: dict[str, str] = {}
    conn = None
    try:
        conn = _connect_ro(db_path)
        conn.execute("SELECT 1 FROM kv LIMIT 1")
    except sqlite3.Error as err:
        if conn is not None:
            conn.close()
        conn = None
        db_error = f"(the database could not be read: {err})\n"
    try:
        def section(name, fn):
            if conn is None:
                files[name] = db_error
                return
            try:
                files[name] = fn()
            except Exception as err:            # one section failing never loses the others
                files[name] = f"(this section failed: {type(err).__name__}: {err})\n"

        section("status.md", lambda: status.markdown(
            status.status_view(conn, now, ctx.tz, notes=owner_notes), ctx.tz) + "\n")
        section("health.json", lambda: _dump(redact(health.health_view(conn, now, ctx.tz), include_position)))
        section("findings.json", lambda: _dump(redact({
            "now": api.findings_view(conn, ctx, now, notes=owner_notes),
            "log": findings_log.history(conn, owner_notes)}, include_position)))
        section("events.json", lambda: _dump(redact(events(conn, now), include_position)))
        files["box.json"] = _dump(redact(box_health(conn, now, db_path), include_position))
        if include_position and conn is not None:
            pos = _kv(conn, location.KV_POSITION)
            files["position.json"] = _dump(json.loads(pos) if pos else None)
    finally:
        if conn is not None:
            conn.close()
    files.update(config_files(config_path, include_position))
    for name, text in service_logs(config_path, directory=log_dir).items():
        files[f"logs/{name}"] = text
    secrets = known_secrets(config_path)
    files = {k: scrub_text(v, secrets, include_position or k.endswith(".json")) for k, v in files.items()}
    files["README.txt"] = readme(now, include_position, sorted(files))
    return files


def readme(now: int, include_position: bool, names: list[str]) -> str:
    when = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Cerberus support file ({FORMAT} v{VERSION}), made {when}.", "",
             "Files:"] + [f"  {n}" for n in names + ["README.txt"]] + [
             "", "Never included: " + "; ".join(NEVER) + ".",
             "Secrets in settings and logs are replaced with " + REMOVED + ".",
             "Position: " + ("included (the owner ticked the box)." if include_position else
                             "left out (lat/lon, home ZIP and coordinates removed)."), ""]
    return "\n".join(lines)


def zip_bytes(files: dict[str, str], now: int) -> bytes:
    buf = io.BytesIO()
    stamp = time.gmtime(now)[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"cerberus-support/{name}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])
    return buf.getvalue()


def filename(device_id: str | None, now: int) -> str:
    tag = re.sub(r"[^A-Za-z0-9-]", "", (device_id or "box"))[:8] or "box"
    return f"cerberus-support-{tag}-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.zip"


def preview(config_path: str | Path, include_position: bool = False) -> dict:
    """What the owner sees before downloading: the sections, and the config with secrets removed."""
    secrets = known_secrets(config_path)
    cf = config_files(config_path, include_position)
    return {"sections": [{"name": n, "what": w} for n, w in SECTIONS],
            "never": list(NEVER), "position_included": include_position,
            "config": {k: scrub_text(v, secrets, include_position) for k, v in cf.items()}}
