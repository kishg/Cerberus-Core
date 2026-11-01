"""Db from several threads at once (one connection per thread).

The collector uses its Db from the paho callback thread and the main loop
concurrently. With one shared connection those calls interleaved mid-statement;
on 2026-10-04 18:18:23 a detector kv read failed with
``InterfaceError: bad parameter or other API misuse`` in the same second the
main loop wrote a cabin row. This reproduces that shape: one thread doing kv
reads (as the detectors do) while another inserts cabin rows (as the Govee
cloud tick does).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

from cerberus.db import Db

DURATION_S = 1.5


def _db() -> Db:
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_each_thread_gets_its_own_connection():
    db = _db()
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.conn))
    t.start()
    t.join()
    assert seen[0] is not db.conn
    assert db.conn is db.conn  # stable within a thread


def test_concurrent_reads_and_writes_do_not_misuse_the_connection():
    """Insert methods swallow sqlite errors and return False, so a collision
    is a silently dropped sample, not an exception. Count both. On the old
    shared connection this lost thousands of writes per second of contention."""
    db = _db()
    db.set_kv("last_full_charge_ts", "1")
    errors: list[BaseException] = []
    dropped = [0]
    stop = time.monotonic() + DURATION_S

    def reader():
        try:
            while time.monotonic() < stop:
                db.get_kv("last_full_charge_ts")
                db.conn.execute(
                    "SELECT ts, air_temp_f FROM cabin_samples ORDER BY ts DESC LIMIT 50"
                ).fetchall()
        except BaseException as e:  # pragma: no cover - the failure being tested
            errors.append(e)

    def writer(base):
        i = 0
        while time.monotonic() < stop:
            if not db.insert_cabin_sample(base + i, "cabin", 64.0, 60.0, None,
                                          source="govee_cloud"):
                dropped[0] += 1
            i += 1

    threads = ([threading.Thread(target=reader) for _ in range(3)]
               + [threading.Thread(target=writer, args=(b * 10**7,)) for b in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert dropped[0] == 0, f"{dropped[0]} cabin rows silently dropped"


def test_close_closes_every_threads_connection():
    db = _db()
    other = []
    t = threading.Thread(target=lambda: other.append(db.conn))
    t.start()
    t.join()
    main = db.conn
    db.close()
    for c in (main, other[0]):
        try:
            c.execute("SELECT 1")
            raise AssertionError("connection still open after close()")
        except Exception as e:
            assert "closed" in str(e)
    # Touching the Db after close opens a fresh connection instead of failing.
    assert db.conn.execute("SELECT 1").fetchone() == (1,)
