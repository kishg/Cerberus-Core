"""Tests for the store-and-forward sync layer (M3).

The contract we're proving:
- Successful POSTs advance ``sync_state`` cursors monotonically by ROWID.
- Failed POSTs leave the cursor untouched — same rows retry next tick.
- No row is ever sent twice.
- Disabled syncer never POSTs.
"""

from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch

import pytest

from cerberus.config import SyncConfig
from cerberus.db import Db
from cerberus.events import Event, Severity
from cerberus.model import PackState
from cerberus.syncer import Syncer


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        d = Db(os.path.join(tmp, "c.db"))
        yield d
        d.close()


def _seed_samples(db: Db, n: int, base_ts: int = 1_000_000) -> None:
    samples = [
        PackState(
            pack_id=(i % 3) + 1, uc=f"uc11455{i:02d}", instance=(i % 3) + 1,
            soc=80 + i, voltage=13.2, current=-1.0, power=-13,
            temperature=28.0, timetogo=0, ts=base_ts + i,
        )
        for i in range(n)
    ]
    db.insert_pack_samples(samples)


def _seed_events(db: Db, n: int, base_ts: int = 2_000_000) -> None:
    for i in range(n):
        db.insert_event(
            Event(ts=base_ts + i, type=f"e{i}", severity=Severity.INFO, detail={"i": i})
        )


def _cfg(enabled: bool = True, batch: int = 100) -> SyncConfig:
    return SyncConfig(
        enabled=enabled,
        target_url="http://example.test/ingest",
        auth_token="",
        batch_size=batch,
        interval_seconds=60,
        request_timeout_seconds=5,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_tick_sends_rows_then_advances_cursor(db):
    _seed_samples(db, 5)
    _seed_events(db, 3)
    s = Syncer(_cfg(), db)

    posted: list[tuple[str, list[dict]]] = []

    def fake_post(table, rows):
        posted.append((table, list(rows)))
        return True

    with patch.object(s, "_post", side_effect=fake_post):
        reports = s.tick()

    tables = {r.table: r for r in reports}
    assert tables["battery_samples"].sent == 5
    assert tables["events"].sent == 3
    # cursors moved to the largest rowid sent
    assert db.get_last_synced("battery_samples") == tables["battery_samples"].new_cursor
    assert db.get_last_synced("events") == tables["events"].new_cursor
    # POSTed payloads carry the actual rows
    assert {t for t, _ in posted} == {"battery_samples", "events"}
    sample_rows = next(rows for t, rows in posted if t == "battery_samples")
    assert sample_rows[0]["soc"] == 80.0


def test_subsequent_tick_sends_only_new_rows(db):
    _seed_samples(db, 4)
    s = Syncer(_cfg(), db)

    with patch.object(s, "_post", return_value=True):
        s.tick()
    assert db.get_last_synced("battery_samples") > 0
    cursor_after_first = db.get_last_synced("battery_samples")

    posted_rows: list[list[dict]] = []

    def fake_post(table, rows):
        if table == "battery_samples":
            posted_rows.append(list(rows))
        return True

    # add 2 new samples; only the new ones should be sent
    _seed_samples(db, 2, base_ts=9_999_000)
    with patch.object(s, "_post", side_effect=fake_post):
        s.tick()

    assert len(posted_rows) == 1
    assert len(posted_rows[0]) == 2
    assert db.get_last_synced("battery_samples") > cursor_after_first


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_failed_post_does_not_advance_cursor(db):
    _seed_samples(db, 3)
    s = Syncer(_cfg(), db)

    with patch.object(s, "_post", return_value=False):
        s.tick()

    # cursor still 0
    assert db.get_last_synced("battery_samples") == 0

    # next tick succeeds and sends the SAME rows
    posted: list[list[dict]] = []

    def good_post(table, rows):
        if table == "battery_samples":
            posted.append(list(rows))
        return True

    with patch.object(s, "_post", side_effect=good_post):
        s.tick()

    assert len(posted) == 1 and len(posted[0]) == 3
    assert db.get_last_synced("battery_samples") > 0


def test_idempotency_no_double_send_after_outage(db):
    """Simulate a network outage spanning multiple ticks: rows accumulate but
    none are sent, then connectivity returns and ALL queued rows go exactly
    once. No row ever appears twice across all POSTs in this scenario."""
    s = Syncer(_cfg(), db)
    all_posted: list[dict] = []

    # ---- outage: 3 ticks each fail, 4 rows added per tick ----
    for i in range(3):
        _seed_samples(db, 4, base_ts=1_000_000 + i * 1000)
        with patch.object(s, "_post", return_value=False):
            s.tick()
    assert db.get_last_synced("battery_samples") == 0

    # ---- recovery: one successful tick should drain everything ----
    def fake_post(table, rows):
        if table == "battery_samples":
            all_posted.extend(rows)
        return True

    with patch.object(s, "_post", side_effect=fake_post):
        s.tick()

    # 12 rows accumulated, all sent exactly once
    assert len(all_posted) == 12
    # uniqueness: a (ts, pack_id) tuple identifies a sample uniquely
    keys = {(r["ts"], r["pack_id"]) for r in all_posted}
    assert len(keys) == 12


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_disabled_syncer_never_starts_thread(db):
    s = Syncer(_cfg(enabled=False), db)
    with patch.object(s, "_post") as p:
        s.start()
        time.sleep(0.05)
        s.stop()
        p.assert_not_called()
    assert s._thread is None


def test_batch_size_limits_rows_per_tick(db):
    _seed_samples(db, 10)
    s = Syncer(_cfg(batch=4), db)

    sent_counts: list[int] = []

    def fake_post(table, rows):
        if table == "battery_samples":
            sent_counts.append(len(rows))
        return True

    with patch.object(s, "_post", side_effect=fake_post):
        s.tick()
        s.tick()
        s.tick()  # one extra; nothing left to send

    assert sent_counts == [4, 4, 2]
    assert db.get_last_synced("battery_samples") > 0
