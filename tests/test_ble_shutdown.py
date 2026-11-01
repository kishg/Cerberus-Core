"""Sidecar loops must stop when asked.

Diagnosed 2026-09-30 while smoke-testing the new OBD loop against a fabricated
BLE address: ``BleakClient.connect()`` never returns on macOS when
CoreBluetooth cannot resolve the address, and it does not honour bleak's own
discovery timeout. Every loop here used a bare ``async with BleakClient(...)``
plus bare ``asyncio.sleep``, so a SIGTERM arriving mid-connect hung the whole
process until launchd escalated to SIGKILL — and a SIGTERM arriving mid-sleep
held it for the rest of the interval.

That matters more than it sounds. This sidecar is out of BLE range most of the
time (the laptop lives in the house, the packs in the van), so "stuck in a
connect that will never succeed" is its normal state, not an edge case.

The connect bound is 60 s rather than 30 because the log shows a *successful*
pack connect taking 33.3 s — 2026-09-21 14:35:42 -> 14:36:15 on pack 2 — while
out-of-range failures land at exactly 30.0 s. A good connect can outlast
bleak's own timeout, so anything at or below 30 s would have broken a connect
that works today.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from cerberus.ble import BleCollector
from cerberus.config import BleConfig
from cerberus.db import Db

PACK = "00000000-0000-4000-8000-000000000001"


def _ble(**over):
    kw = dict(enabled=True, pack_addresses={1: PACK}, cabin_sensors={},
              cabin_sample_seconds=60, sample_downsample_seconds=30,
              reconnect_backoff_seconds=1, connect_timeout_seconds=0.2)
    kw.update(over)
    return BleConfig(**kw)


def _db():
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


async def _time_shutdown(collector, settle=0.3, limit=5):
    """Let the collector get going, then time how long shutdown takes."""
    task = asyncio.create_task(collector.run())
    await asyncio.sleep(settle)
    loop = asyncio.get_running_loop()
    collector.shutdown()
    t0 = loop.time()
    await asyncio.wait_for(task, timeout=limit)
    return loop.time() - t0


class _Hanging:
    """A connect that never returns, like CoreBluetooth on an unknown address."""

    def __init__(self, *a, **k):
        pass

    async def connect(self):
        await asyncio.Event().wait()

    async def disconnect(self):
        pass


class _Connected:
    """Connects instantly and stays up, so the loop sits in its poll sleep."""

    def __init__(self, *a, **k):
        self.is_connected = True

    async def connect(self):
        pass

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, *a, **k):
        pass

    async def stop_notify(self, *a, **k):
        pass


def test_pack_loop_shutdown_survives_a_hanging_connect(monkeypatch):
    """The regression. Out of range is this sidecar's normal state, so a
    shutdown landing mid-connect is the likely case, not a rare one."""
    monkeypatch.setattr("cerberus.ble.BleakClient", _Hanging)
    c = BleCollector(_ble(), _db())
    assert asyncio.run(_time_shutdown(c)) < 2.0


def test_pack_loop_shutdown_is_not_held_by_the_poll_sleep(monkeypatch):
    """While connected the loop polls on a 2 s sleep. A bare sleep there costs
    up to 2 s of shutdown latency; waiting on the stop event costs none."""
    monkeypatch.setattr("cerberus.ble.BleakClient", _Connected)
    c = BleCollector(_ble(), _db())
    # 0.1 s in, so ~1.9 s of the old bare sleep(2.0) would still be pending.
    assert asyncio.run(_time_shutdown(c, settle=0.1)) < 0.5


def test_cabin_loop_shutdown_is_not_held_by_its_scan_sleep(monkeypatch):
    """Same 2 s sleep in the passive scanner loop."""
    class _Scanner:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr("cerberus.ble.BleakScanner", _Scanner)
    monkeypatch.setattr("cerberus.ble.BleakClient", _Connected)
    c = BleCollector(_ble(cabin_sensors={"AA": "cabin"}), _db())
    assert asyncio.run(_time_shutdown(c, settle=0.1)) < 0.5


def test_connect_timeout_default_clears_the_slowest_real_connect():
    """Pins the 60 s default against the evidence it came from: a successful
    pack connect has taken 33.3 s. A 30 s bound would have killed it."""
    assert BleConfig(
        enabled=True, pack_addresses={}, cabin_sensors={},
        cabin_sample_seconds=60, sample_downsample_seconds=30,
        reconnect_backoff_seconds=5,
    ).connect_timeout_seconds >= 33.3 * 1.5
