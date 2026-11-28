"""Opt-in box health reports: is this box alive and collecting?

Off by default. When ``health_report.enabled`` is on (beta boxes, under the
beta agreement), the collector POSTs a small report every
``interval_seconds`` to ``target_url`` with a bearer token, so the people
supporting the box notice a dead, offline, full or overheating box without
waiting for the owner.

**Box health only, never van data.** A report says what the box is (device
ID, Core version), how it is (uptime, restarts, disk, memory, CPU
temperature, the state of its own Wi-Fi network) and whether each data source is arriving (status and age, from
``cerberus.health``), plus today's coverage percentage. It carries no
readings (no voltages, currents, temperatures of the van, state of charge)
and no position. ``build()`` assembles it from an allowlist, and
tests/test_healthreport.py fails if anything else appears.

**Latest state only.** A missed report is not backfilled. The next one says
how long it had been since the last one got through (``since_last``), and how
much of that time the collector itself was down (from ``collector_uptime``).

The last attempt (payload, time, result) is kept in ``kv`` so Settings can
show the owner exactly what is sent.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import tzinfo
from pathlib import Path

from . import health, wifiap
from .config import HealthReportConfig
from .db import Db

log = logging.getLogger(__name__)

FORMAT = "cerberus.health"
VERSION = 1
KV_LAST = "health_report.last"          # the last attempt: payload, time, result
KV_LAST_OK = "health_report.last_ok"    # unix time of the last report that got through
UPTIME_PERIOD = 300                     # matches the collector's collector_uptime cadence


def core_version() -> str:
    try:
        return importlib.metadata.version("cerberus")
    except importlib.metadata.PackageNotFoundError:
        from . import __version__
        return __version__


# -- the host -------------------------------------------------------------------

def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def host_uptime_s() -> int | None:
    text = _read("/proc/uptime")
    try:
        return int(float(text.split()[0])) if text else None
    except (ValueError, IndexError):
        return None


def memory() -> dict | None:
    text = _read("/proc/meminfo")
    if not text:
        return None
    vals = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            try:
                vals[key] = int(rest.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    if "MemTotal" not in vals:
        return None
    return {"total_bytes": vals["MemTotal"], "available_bytes": vals.get("MemAvailable")}


def cpu_temp_c(root: str = "/sys/class/thermal") -> float | None:
    """The hottest CPU-ish thermal zone, or the hottest zone at all."""
    zones = []
    for z in sorted(Path(root).glob("thermal_zone*")):
        kind = (_read(str(z / "type")) or "").strip()
        raw = _read(str(z / "temp"))
        try:
            c = int(raw.strip()) / 1000.0 if raw else None
        except ValueError:
            c = None
        if c is not None and -40 < c < 150:
            zones.append((kind, c))
    if not zones:
        return None
    cpu = [c for k, c in zones if "pkg" in k or "cpu" in k.lower() or "x86" in k]
    return round(max(cpu or [c for _, c in zones]), 1)


def disk(path: str) -> dict | None:
    try:
        u = shutil.disk_usage(Path(path).parent if Path(path).suffix else path)
    except OSError:
        return None
    return {"total_bytes": u.total, "free_bytes": u.free}


# -- what the database says -----------------------------------------------------

def collector_down_s(conn: sqlite3.Connection, since: int, now: int) -> int | None:
    """Seconds between `since` and `now` with no collector_uptime mark: the
    collector itself was not running (box off, asleep, crashed)."""
    try:
        marks = [r[0] for r in conn.execute(
            "SELECT ts FROM collector_uptime WHERE ts >= ? AND ts <= ? ORDER BY ts", (since, now))]
    except sqlite3.Error:
        return None
    down, prev = 0, since
    for ts in marks + [now]:
        if ts - prev > 2 * UPTIME_PERIOD:
            down += ts - prev - UPTIME_PERIOD
        prev = ts
    return down


def restarts_since(conn: sqlite3.Connection, since: int) -> int | None:
    try:
        row = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'collector_started' AND ts > ?",
                           (since,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def gx_link(conn: sqlite3.Connection, now: int) -> dict | None:
    """The GX link from the collector's live view (#66): up or down, how long, why, and the cloud
    fallback. Times only: never an address."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = 'live.snapshot'").fetchone()
        gx = (json.loads(row[0]) or {}).get("gx") if row else None
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(gx, dict):
        return None
    down = gx.get("lan") == "down"
    last = gx.get("lan_last")
    return {"lan": gx.get("lan"), "cause": gx.get("cause") if down else None,
            "down_s": max(0, now - int(last)) if down and isinstance(last, (int, float)) else None,
            "fallback": gx.get("fallback") if down else None}


# -- the report -----------------------------------------------------------------

def build(conn: sqlite3.Connection, now: int, *, device_id: str, interval_s: int,
          started_at: int, last_ok_at: int | None, db_path: str,
          tz: tzinfo | None = None, thermal_root: str = "/sys/class/thermal",
          wifi_status_dir: Path = wifiap.STATUS_DIR) -> dict:
    """The report, from an allowlist. Nothing about the van's readings or position."""
    since = last_ok_at if last_ok_at else max(started_at, now - 86400)
    sources = []
    for s in health.sources(conn, now):
        sources.append({"key": s["key"], "status": s["status"], "age_s": int(s["age_s"])})
    coverage = health.coverage(conn, now, tz, days=1)
    return {
        "format": FORMAT,
        "version": VERSION,
        "box": {"id": device_id, "core_version": core_version()},
        "sent_at": now,
        "interval_s": interval_s,
        "uptime": {"collector_s": max(0, now - started_at), "host_s": host_uptime_s()},
        "since_last": {
            "last_ok_at": last_ok_at,
            "silent_s": (now - last_ok_at) if last_ok_at else None,
            "collector_restarts": restarts_since(conn, since),
            "collector_down_s": collector_down_s(conn, since, now),
        },
        "system": {
            "disk": disk(db_path),
            "memory": memory(),
            "cpu_temp_c": cpu_temp_c(thermal_root),
        },
        "sources": sources,
        "gx": gx_link(conn, now),
        # The box's own Wi-Fi network (Core #53): state, security, channel, how many phones. No name or address.
        "wifi": wifiap.health(wifiap.read_status(wifi_status_dir), now),
        "coverage_today_pct": coverage[-1][1] if coverage else None,
    }


class Reporter:
    """Background sender. start()/stop() lifecycle; a no-op when disabled."""

    def __init__(self, cfg: HealthReportConfig, db: Db, *, device_id: str, db_path: str,
                 tz: tzinfo | None = None, started_at: int | None = None):
        self.cfg = cfg
        self.db = db
        self.db_path = db_path
        self.device_id = device_id
        self.tz = tz
        self.started_at = started_at or int(time.time())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("health reports off (health_report.enabled=false)")
            return
        self._thread = threading.Thread(target=self._loop, name="cerberus-health-report", daemon=True)
        self._thread.start()
        log.info("health reports to %s every %ss", urllib.parse.urlsplit(self.cfg.target_url).hostname, self.cfg.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        # Report soon after start: a restart is exactly when we want to hear.
        if self._stop.wait(30):
            return
        while True:
            try:
                self.send_once()
            except Exception:
                log.exception("health report failed")
            if self._stop.wait(self.cfg.interval_seconds):
                return

    def send_once(self, now: int | None = None) -> bool:
        now = now or int(time.time())
        last_ok = self.db.get_kv(KV_LAST_OK)
        last_ok_at = int(last_ok) if last_ok and last_ok.isdigit() else None
        report = build(self.db.conn, now, device_id=self.device_id,
                       interval_s=self.cfg.interval_seconds, started_at=self.started_at,
                       last_ok_at=last_ok_at, db_path=self.db_path, tz=self.tz)
        ok, result = self._post(report)
        self.db.set_kv(KV_LAST, json.dumps({"at": now, "ok": ok, "result": result, "payload": report}))
        if ok:
            self.db.set_kv(KV_LAST_OK, str(now))
        else:
            log.warning("health report not delivered: %s", result)
        return ok

    def _post(self, report: dict) -> tuple[bool, str]:
        headers = {"Content-Type": "application/json", "User-Agent": f"cerberus/{core_version()}"}
        if self.cfg.auth_token:
            headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        req = urllib.request.Request(self.cfg.target_url, data=json.dumps(report).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as resp:
                status = getattr(resp, "status", resp.getcode())
                return 200 <= status < 300, f"HTTP {status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return False, f"not reachable: {getattr(e, 'reason', e)}"
