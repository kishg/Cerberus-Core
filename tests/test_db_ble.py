"""DB tests for the new BLE-sourced tables and the latest_ble_per_pack helper."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from cerberus.db import BlePackSample, Db


@pytest.fixture()
def db():
    with tempfile.TemporaryDirectory() as d:
        yield Db(str(Path(d) / "test.db"))


def test_insert_and_read_cell_samples(db):
    n = db.insert_cell_samples(1000, 3, [3300, 3305, 3310, 3315])
    assert n == 4
    rows = list(db.conn.execute(
        "SELECT cell_idx, voltage_mv FROM cell_samples "
        "WHERE pack_id=? ORDER BY cell_idx", (3,)))
    assert rows == [(1, 3300), (2, 3305), (3, 3310), (4, 3315)]


def test_insert_and_read_pack_ble_sample(db):
    db.insert_pack_ble_sample(
        ts=1000, pack_id=3, pack_voltage_cv=1336, soc_pct=99, current_a=-1,
        temp_dc=306, big_counter=589413, field_a5=93, field_a6=86,
        flags_hex="000000", config_hex="FF03",
    )
    row = db.conn.execute("SELECT * FROM pack_ble_samples").fetchone()
    assert row[0] == 1000  # ts
    assert row[1] == 3     # pack_id
    assert row[2] == 1336  # pack_voltage_cv


def test_latest_ble_per_pack_returns_most_recent(db):
    # two timestamps per pack — helper should pick the newest per pack
    for pid in (1, 2, 3):
        db.insert_cell_samples(1000, pid, [3300, 3300, 3300, 3300])
        db.insert_pack_ble_sample(
            ts=1000, pack_id=pid, pack_voltage_cv=1320, soc_pct=95, current_a=-2,
            temp_dc=300, big_counter=10_000, field_a5=80, field_a6=72,
            flags_hex="000000", config_hex="FF03",
        )
        db.insert_cell_samples(2000, pid, [3340 + pid, 3340, 3340, 3340])
        db.insert_pack_ble_sample(
            ts=2000, pack_id=pid, pack_voltage_cv=1340, soc_pct=99, current_a=0,
            temp_dc=305, big_counter=20_000, field_a5=85, field_a6=75,
            flags_hex="000000", config_hex="FF04",
        )
    out = db.latest_ble_per_pack()
    assert set(out.keys()) == {1, 2, 3}
    for pid, sample in out.items():
        assert sample.ts == 2000
        assert sample.soc_pct == 99
        assert sample.config_hex == "FF04"
        assert sample.cells_mv == [3340 + pid, 3340, 3340, 3340]


def test_latest_ble_per_pack_freshness_filter(db):
    now = int(time.time())
    # Stale row from 5 minutes ago
    db.insert_pack_ble_sample(
        ts=now - 300, pack_id=1, pack_voltage_cv=1300, soc_pct=80, current_a=-5,
        temp_dc=290, big_counter=1000, field_a5=70, field_a6=70,
        flags_hex="000000", config_hex="FF03",
    )
    db.insert_cell_samples(now - 300, 1, [3250, 3250, 3250, 3250])
    # With 60-second window, helper should return empty
    assert db.latest_ble_per_pack(max_age_seconds=60) == {}
    # Without window, includes it
    assert set(db.latest_ble_per_pack().keys()) == {1}


def test_latest_ble_per_pack_empty(db):
    assert db.latest_ble_per_pack() == {}


# ---------------------------------------------------------------------------
# Concurrent writers — the collector and the BLE sidecar share one file
# ---------------------------------------------------------------------------

def test_busy_timeout_is_set(db):
    (v,) = db.conn.execute("PRAGMA busy_timeout").fetchone()
    assert v == Db.BUSY_TIMEOUT_MS


def test_second_writer_waits_instead_of_losing_the_sample():
    """Without a busy timeout SQLite raises 'database is locked' the instant
    two writers collide and the losing sample is dropped. WAL does not help —
    it decouples readers from writers, not writers from each other."""
    import threading

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "contended.db")
        holder = Db(path)
        writer = Db(path)

        holder.conn.execute("BEGIN IMMEDIATE")
        holder.conn.execute(
            "INSERT INTO cell_samples (ts, pack_id, cell_idx, voltage_mv) "
            "VALUES (1, 1, 0, 3300)")

        result: list = []

        def contend():
            try:
                writer.insert_cell_samples(2, 2, [3300, 3301, 3302, 3303])
                result.append("ok")
            except Exception as e:  # pragma: no cover - failure path
                result.append(e)

        t = threading.Thread(target=contend)
        t.start()
        time.sleep(0.2)          # writer is now blocked on the held lock
        assert result == [], "should still be waiting, not failed"
        holder.conn.execute("COMMIT")
        t.join(timeout=5)

        assert result == ["ok"], f"second writer lost its sample: {result}"
        rows = list(writer.conn.execute(
            "SELECT COUNT(*) FROM cell_samples WHERE pack_id=2"))
        assert rows[0][0] == 4
        holder.close()
        writer.close()
