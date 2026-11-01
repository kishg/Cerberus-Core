"""Lithionics ACR stream capture.

The only frame decoded so far was captured parked with the contactor open
(2026-09-24): "*,0,0,1261,1325,0,0,0,1,6002". Fields 1, 2 and 5-7 are
unmapped, so the writer keeps every one of them plus the raw line, and writes
at once on any change in them so a contactor event is not averaged away by
the 30 s throttle.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from cerberus import ble as ble_mod
from cerberus.ble import BleCollector, parse_acr_frame
from cerberus.config import BleConfig
from cerberus.db import Db

PARKED = "*,0,0,1261,1325,0,0,0,1,6002"
ACR = "00000000-0000-4000-8000-0000000000A1"


def _collector(**over):
    kw = dict(enabled=True, pack_addresses={}, cabin_sensors={},
              cabin_sample_seconds=60, sample_downsample_seconds=30,
              reconnect_backoff_seconds=1, connect_timeout_seconds=0.2,
              acr_address=ACR, acr_sample_seconds=30)
    kw.update(over)
    return BleCollector(BleConfig(**kw), Db(os.path.join(tempfile.mkdtemp(), "t.db")))


def _rows(c):
    return c.db.conn.execute(
        "SELECT ts, field1, chassis_v_cv, house_v_cv, raw FROM acr_samples ORDER BY ts"
    ).fetchall()


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_parses_the_parked_capture():
    f = parse_acr_frame(PARKED + "\r")
    assert f == {
        "raw": PARKED, "field1": 0, "field2": 0, "chassis_v_cv": 1261,
        "house_v_cv": 1325, "field5": 0, "field6": 0, "field7": 0,
        "chassis_present": 1, "version": 6002,
    }


def test_odd_shapes_keep_the_raw_line():
    # A drive may well produce a frame the parked capture never showed.
    short = parse_acr_frame("*,1,x,1290")
    assert short["field1"] == 1 and short["field2"] is None
    assert short["chassis_v_cv"] == 1290 and short["house_v_cv"] is None
    assert short["raw"] == "*,1,x,1290"
    assert parse_acr_frame("*,1,2,3,4,5,6,7,8,9,10,11")["raw"].endswith(",11")


def test_rejects_non_acr_lines():
    for line in ("", "Lithionics ACR v6.0.02", "&,3,306,589413,0144,2300,FF03,0000",
                 "1336,334,334,334,334,80,73,0,99,000000", "ERROR"):
        assert parse_acr_frame(line) is None


def test_steady_frames_are_throttled(monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector()
    for i in range(60):            # a minute of 1/s frames, voltage drifting
        clock.t = 1000 + i
        c._dispatch_acr(f"*,0,0,1261,{1325 + i % 3},0,0,0,1,6002")
    assert [r[0] for r in _rows(c)] == [1000, 1030]


def test_a_change_in_an_unmapped_field_is_written_at_once(monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector()
    c._dispatch_acr(PARKED)
    clock.t = 1003
    c._dispatch_acr("*,1,0,1261,1380,0,0,0,1,6002")   # contactor closes?
    clock.t = 1004
    c._dispatch_acr("*,1,0,1262,1390,0,0,0,1,6002")   # only volts moved
    clock.t = 1005
    c._dispatch_acr(PARKED)                            # and opens again
    assert [(r[0], r[1]) for r in _rows(c)] == [(1000, 0), (1003, 1), (1005, 0)]


def test_unrecognised_lines_are_not_stored(monkeypatch):
    c = _collector()
    c._dispatch_acr("Lithionics ACR v6.0.02")
    c._dispatch_acr("")
    assert _rows(c) == []


def test_collector_runs_with_only_an_acr(monkeypatch):
    """No packs configured must not short-circuit to "nothing to collect"."""
    seen = []

    class _Client:
        def __init__(self, addr, *a, **k):
            seen.append(addr)
            self.is_connected = True

        async def connect(self):
            pass

        async def disconnect(self):
            self.is_connected = False

        async def start_notify(self, _char, cb):
            cb(None, bytearray((PARKED + "\r\n").encode()))

        async def stop_notify(self, *a, **k):
            pass

    monkeypatch.setattr("cerberus.ble.BleakClient", _Client)
    c = _collector()

    async def go():
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.1)
        c.shutdown()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert seen == [ACR]
    assert [r[4] for r in _rows(c)] == [PARKED]


def test_config_defaults_to_no_acr(tmp_path, monkeypatch):
    from cerberus import config as cfg_mod
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "x")
    base = (tmp_path / "c.yaml")
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    base.write_text(src.replace('acr_address: "00000000-0000-4000-8000-0000000000A1"', ""))
    assert cfg_mod.load(base).ble.acr_address == ""
    full = tmp_path / "f.yaml"
    full.write_text(src)
    cfg = cfg_mod.load(full)
    assert cfg.ble.acr_address == ACR and cfg.ble.acr_sample_seconds == 30
