"""Optional off-site store-and-forward sync.

Local-first by design: the DB is the buffer, the network is best-effort. Each
tick we read batches from each synced table starting just after its cursor in
``sync_state``, POST them as JSON, and only advance the cursor on a 2xx
response. A failed POST leaves the cursor untouched, so the same rows are
retried next tick: no duplicates, no losses, no dead-letter queue needed.
Nothing here ever deletes a row, synced or not.

What a batch carries (``cerberus.sync`` v1, Core #43):

- the box's device ID and Core version, and the table;
- ``key``: the columns the receiver upserts on (``Db.SYNC_KEYS``), so a
  re-send replaces the same rows instead of adding copies;
- the rows. **Positions never leave the box** unless ``sync.share_position``
  is on: weather's lat/lon/position_source are dropped, and so is any lat/lon
  inside JSON text in a row.

Order and pace, for vans that are offline for weeks and then catch up over a
weak or metered link:

- **Events and findings first** (``events``, ``finding_log``), every
  ``prompt_seconds``, drained before anything else, so alerts and Co-Pilot
  hear what happened first and promptly.
- **Then samples, oldest first, one batch per table per interval**: the rate
  cap. ``sync.metered`` makes that four intervals and holds the high-rate
  tables (cells, raw Bluetooth, OBD) back.
- ``finding_log`` rows change after they're written (a finding clears), so it
  is followed by a change counter (``sync_seq``), not by rowid.

The owner's notes on findings (``.cerberus-notes.json``) go as one document
whenever they change. So does the van's profile (``van_profile``): what the
system is, its packs and capacity, which data sources are on, and its time
zone, so the Cloud's Co-Pilot reads the data the way the box does (cloud #23).
No position: the time zone is the closest it comes, and it says no more than
the times in every row already do.

We use stdlib urllib so the dependency footprint stays paho + pyyaml.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import HIGH_RATE_SYNC_TABLES, SyncConfig
from .db import Db

log = logging.getLogger(__name__)

FORMAT, VERSION = "cerberus.sync", 1
# Kept for callers that imported it: what sync sends when nothing is configured.
SYNC_TABLES = ("battery_samples", "events")
PROMPT_TABLES = ("events", "finding_log")
PROMPT_MAX_BATCHES = 20                 # per prompt tick: a backlog of events drains quickly
METERED_FACTOR = 4
KV_LAST = "sync.last"
KV_NOTES_HASH = "sync.notes_hash"
KV_PROFILE_HASH = "sync.profile_hash"


def van_profile(config_path: str | Path, kv_timezone: str | None = None) -> dict:
    """The van, as Co-Pilot's tools describe it (cerberus_copilot.tools.open_van), from config.yaml
    and setup.yaml. The time zone is the configured one, else the one the box worked out."""
    from . import config as cfg_mod
    from .profile import LIFEPO4
    cfg, ui = cfg_mod.load(config_path), cfg_mod.load_ui(config_path)
    return {
        "system": f"{cfg.system.nominal_v} V {cfg.system.chemistry}",
        "lithium": cfg.system.chemistry == LIFEPO4,
        "bank_source": cfg.bank.source,
        "pack_count": ui.pack_count,
        "capacity_ah": ui.capacity_ah,
        "time_zone": ui.timezone or kv_timezone,
        "path_limit_mohm": ui.path_limit_mohm,
        "full_remind_days": ui.full_remind_days,
        "data_sources": {
            "victron_gx_mqtt": True,
            "battery_bluetooth": cfg.ble.enabled,
            "obd": bool(getattr(cfg.obd, "enabled", False)),
            "weather_forecast": cfg.weather.enabled,
            "cabin_sensor_cloud": cfg.govee_cloud.enabled,
        },
    }

# Never sent unless sync.share_position is on.
POSITION_COLUMNS = frozenset({"lat", "lon", "position_source"})
POSITION_KEYS = frozenset({"lat", "lon", "latitude", "longitude", "position", "position_source", "gps"})


@dataclass
class SyncReport:
    table: str
    sent: int
    new_cursor: int


def _scrub_json(value):
    if isinstance(value, dict):
        return {k: _scrub_json(v) for k, v in value.items() if k not in POSITION_KEYS}
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    return value


def strip_position(row: dict) -> dict:
    """A copy of the row with every position removed: columns, and lat/lon inside JSON text."""
    out = {}
    for k, v in row.items():
        if k in POSITION_COLUMNS:
            continue
        if isinstance(v, str) and v[:1] in "{[":
            try:
                parsed = json.loads(v)
            except ValueError:
                pass
            else:
                v = json.dumps(_scrub_json(parsed), sort_keys=True)
        out[k] = v
    return out


@dataclass
class _Totals:
    at: int = 0
    ok: bool = True
    result: str = ""
    sent: dict = field(default_factory=dict)


class Syncer:
    """Background sync thread. start()/stop() lifecycle. No-op if disabled."""

    def __init__(self, cfg: SyncConfig, db: Db, *, device_id: str | None = None,
                 core_version: str | None = None, notes_path: str | Path | None = None):
        self.cfg = cfg
        self.db = db
        self.device_id = device_id
        self.core_version = core_version
        self.notes_path = Path(notes_path) if notes_path else None
        self.config_path: Path | None = None        # for the van's profile; set by the collector
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_samples = 0.0

    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("syncer disabled (sync.enabled=false)")
            return
        if not self.cfg.target_url:
            log.error("syncer enabled but target_url empty; skipping start")
            return
        self._thread = threading.Thread(target=self._loop, name="cerberus-syncer", daemon=True)
        self._thread.start()
        log.info("syncer started: %s, tables %s", self.cfg.target_url, ", ".join(self.cfg.tables))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---- pace ----------------------------------------------------------------

    @property
    def sample_interval(self) -> int:
        return self.cfg.interval_seconds * (METERED_FACTOR if self.cfg.metered else 1)

    def _loop(self) -> None:
        # one immediate full tick so a backlog starts catching up on startup
        try:
            self.tick()
        except Exception:
            log.exception("initial syncer tick failed")
        while not self._stop.wait(self.cfg.prompt_seconds):
            try:
                due = time.monotonic() - self._last_samples >= self.sample_interval
                self.tick(samples=due)
            except Exception:
                log.exception("syncer tick failed")

    def prompt_tables(self) -> list[str]:
        return [t for t in self.cfg.tables if t in PROMPT_TABLES]

    def sample_tables(self) -> list[str]:
        tables = [t for t in self.cfg.tables if t not in PROMPT_TABLES]
        if self.cfg.metered:
            tables = [t for t in tables if t not in HIGH_RATE_SYNC_TABLES]
        return tables

    def tick(self, samples: bool = True) -> list[SyncReport]:
        """One pass: events and findings drained first, then (when `samples`) one batch from each
        sample table, then the owner's notes if they changed. A report per table."""
        totals = _Totals(at=int(time.time()))
        reports: list[SyncReport] = []
        for table in self.prompt_tables():
            for _ in range(PROMPT_MAX_BATCHES):
                report = self._sync_table(table, totals)
                reports.append(report)
                if report.sent == 0 or report.sent < self.cfg.batch_size:
                    break
        if samples:
            self._last_samples = time.monotonic()
            for table in self.sample_tables():
                reports.append(self._sync_table(table, totals))
            self._sync_notes(totals)
            self._sync_profile(totals)
        self._remember(totals)
        return self._merge(reports)

    @staticmethod
    def _merge(reports: list[SyncReport]) -> list[SyncReport]:
        """One report per table, summing batches."""
        out: dict[str, SyncReport] = {}
        for r in reports:
            if r.table in out:
                out[r.table].sent += r.sent
                out[r.table].new_cursor = r.new_cursor
            else:
                out[r.table] = SyncReport(r.table, r.sent, r.new_cursor)
        return list(out.values())

    # ---- tables ----------------------------------------------------------------

    def _sync_table(self, table: str, totals: _Totals | None = None) -> SyncReport:
        last = self.db.get_last_synced(table)
        batch = self.db.fetch_since(table, last, self.cfg.batch_size)
        if not batch:
            return SyncReport(table=table, sent=0, new_cursor=last)

        cursors = [c for c, _ in batch]
        rows = [row if self.cfg.share_position else strip_position(row) for _, row in batch]
        ok = self._post(table, rows)
        if not ok:
            # leave the cursor where it is; same batch retries next tick
            log.warning("sync failed for %s; %d rows queued (cursor=%d)", table, len(batch), last)
            if totals is not None:
                totals.ok = False
            return SyncReport(table=table, sent=0, new_cursor=last)

        new_cursor = cursors[-1]
        self.db.set_last_synced(table, new_cursor)
        log.info("synced %d rows from %s (cursor %d → %d)", len(batch), table, last, new_cursor)
        if totals is not None:
            totals.sent[table] = totals.sent.get(table, 0) + len(batch)
        return SyncReport(table=table, sent=len(batch), new_cursor=new_cursor)

    def payload(self, table: str, rows: list[dict]) -> dict:
        return {"format": FORMAT, "version": VERSION,
                "box": {"id": self.device_id, "core_version": self.core_version},
                "table": table, "key": list(Db.SYNC_KEYS[table]), "rows": rows}

    def _post(self, table: str, rows: list[dict]) -> bool:
        return self._send(self.payload(table, rows), table)

    # ---- notes -----------------------------------------------------------------

    def _sync_notes(self, totals: _Totals | None = None) -> None:
        """The owner's notes, as one document, whenever the file changed."""
        if not self.notes_path or not self.notes_path.exists():
            return
        try:
            text = self.notes_path.read_text()
            raw = json.loads(text) if text.strip() else {}
            document = raw.get("notes", []) if isinstance(raw, dict) else []   # notes.py's {"notes": [...]}
        except (OSError, ValueError) as e:
            log.warning("notes not synced: %s", e)
            return
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.db.get_kv(KV_NOTES_HASH) == digest:
            return
        body = {"format": FORMAT, "version": VERSION,
                "box": {"id": self.device_id, "core_version": self.core_version},
                "kind": "notes", "document": document}
        if self._send(body, "notes"):
            self.db.set_kv(KV_NOTES_HASH, digest)
            if totals is not None:
                totals.sent["notes"] = 1
        elif totals is not None:
            totals.ok = False

    def _sync_profile(self, totals: _Totals | None = None) -> None:
        """The van's profile, as one document, whenever it changed."""
        if not self.config_path:
            return
        try:
            from .location import KV_TIMEZONE
            document = van_profile(self.config_path, self.db.get_kv(KV_TIMEZONE))
        except Exception as e:                       # a config being edited: try again next time
            log.warning("profile not synced: %s", e)
            return
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        if self.db.get_kv(KV_PROFILE_HASH) == digest:
            return
        body = {"format": FORMAT, "version": VERSION,
                "box": {"id": self.device_id, "core_version": self.core_version},
                "kind": "profile", "document": document}
        if self._send(body, "profile"):
            self.db.set_kv(KV_PROFILE_HASH, digest)
            if totals is not None:
                totals.sent["profile"] = 1
        elif totals is not None:
            totals.ok = False

    # ---- the wire --------------------------------------------------------------

    def _send(self, body: dict, what: str) -> bool:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cfg.auth_token:
            headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        req = urllib.request.Request(self.cfg.target_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as resp:
                status = getattr(resp, "status", resp.getcode())
                self._result = f"HTTP {status}"
                return 200 <= status < 300
        except urllib.error.HTTPError as e:
            self._result = f"HTTP {e.code}"
            log.debug("sync HTTPError %s for %s: %s", e.code, what, e.reason)
            return False
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            self._result = f"not reachable: {getattr(e, 'reason', e)}"
            log.debug("sync network error for %s: %s", what, e)
            return False

    def _remember(self, totals: _Totals) -> None:
        """The last pass, for Settings: when, whether it all got through, and what was sent."""
        if not totals.sent and totals.ok:
            prev = self.db.get_kv(KV_LAST)
            if prev:
                # nothing to send: keep the last real result, refresh only the check time
                try:
                    last = json.loads(prev)
                    last["checked_at"] = totals.at
                    self.db.set_kv(KV_LAST, json.dumps(last))
                    return
                except ValueError:
                    pass
        self.db.set_kv(KV_LAST, json.dumps({
            "at": totals.at, "checked_at": totals.at, "ok": totals.ok,
            "result": getattr(self, "_result", ""), "sent": totals.sent}))

    def waiting(self) -> list[dict]:
        return backlog(self.db.conn, self.cfg)


def backlog(conn, cfg: SyncConfig) -> list[dict]:
    """For each configured table: rows not yet sent, how old the oldest is, and whether a metered
    connection is holding it back. Works on the UI's read-only connection."""
    from .db import sync_cursor, sync_waiting
    out = []
    for table in cfg.tables:
        n, oldest = sync_waiting(conn, table, sync_cursor(conn, table))
        out.append({"table": table, "waiting": n, "oldest_ts": oldest,
                    "held": cfg.metered and table in HIGH_RATE_SYNC_TABLES})
    return out
