"""Optional off-site store-and-forward sync.

Local-first by design: the DB is the buffer, the network is best-effort. Each
tick we read ROWID-ordered batches from each synced table starting just after
``sync_state.last_synced_rowid``, POST them as JSON, and only advance the
cursor on a 2xx response. A failed POST leaves the cursor untouched, so the
same rows are retried next tick — no duplicates, no losses, no dead-letter
queue needed.

We use stdlib urllib so the dependency footprint stays paho + pyyaml.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

from .config import SyncConfig
from .db import Db

log = logging.getLogger(__name__)

SYNC_TABLES = ("battery_samples", "events")


@dataclass
class SyncReport:
    table: str
    sent: int
    new_cursor: int


class Syncer:
    """Background sync thread. start()/stop() lifecycle. No-op if disabled."""

    def __init__(self, cfg: SyncConfig, db: Db):
        self.cfg = cfg
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("syncer disabled (sync.enabled=false)")
            return
        if not self.cfg.target_url:
            log.error("syncer enabled but target_url empty; skipping start")
            return
        self._thread = threading.Thread(target=self._loop, name="cerberus-syncer", daemon=True)
        self._thread.start()
        log.info("syncer started: %s every %ss", self.cfg.target_url, self.cfg.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ---- internals ---------------------------------------------------------

    def _loop(self) -> None:
        # one immediate tick so backlog catches up sooner on startup
        try:
            self.tick()
        except Exception:
            log.exception("initial syncer tick failed")
        while not self._stop.wait(self.cfg.interval_seconds):
            try:
                self.tick()
            except Exception:
                log.exception("syncer tick failed")

    def tick(self) -> list[SyncReport]:
        """Run one sync pass for every synced table. Returns a report per
        table (sent=0 when nothing to do)."""
        reports: list[SyncReport] = []
        for table in SYNC_TABLES:
            report = self._sync_table(table)
            if report is not None:
                reports.append(report)
        return reports

    def _sync_table(self, table: str) -> SyncReport | None:
        last = self.db.get_last_synced(table)
        batch = self.db.fetch_since(table, last, self.cfg.batch_size)
        if not batch:
            return SyncReport(table=table, sent=0, new_cursor=last)

        rowids = [rid for rid, _ in batch]
        payload_rows = [row for _, row in batch]
        ok = self._post(table, payload_rows)
        if not ok:
            # leave the cursor where it is; same batch retries next tick
            log.warning("sync failed for %s; %d rows queued (cursor=%d)",
                        table, len(batch), last)
            return SyncReport(table=table, sent=0, new_cursor=last)

        new_cursor = rowids[-1]
        self.db.set_last_synced(table, new_cursor)
        log.info("synced %d rows from %s (rowid %d → %d)",
                 len(batch), table, last, new_cursor)
        return SyncReport(table=table, sent=len(batch), new_cursor=new_cursor)

    def _post(self, table: str, rows: list[dict]) -> bool:
        data = json.dumps({"table": table, "rows": rows}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cfg.auth_token:
            headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        req = urllib.request.Request(
            self.cfg.target_url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as resp:
                status = getattr(resp, "status", resp.getcode())
                return 200 <= status < 300
        except urllib.error.HTTPError as e:
            log.debug("sync HTTPError %s for %s: %s", e.code, table, e.reason)
            return False
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log.debug("sync network error for %s: %s", table, e)
            return False
