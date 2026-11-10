"""Lithionics ACR stream capture.

The only frame decoded so far was captured parked with the contactor open
(2026-09-24): "*,0,0,1261,1325,0,0,0,1,6002". Fields 1, 2 and 5-7 are
unmapped, so the writer keeps every one of them plus the raw line, and writes
at once on any change in them so a contactor event is not averaged away by
the 30 s throttle.
"""

from __future__ import annotations

import asyncio
import json
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


# ---- $LIST settings read-back --------------------------------------------

LIST_6005 = [
    "Lithionics ACR v6.0.05", "Version=6005", "Chassis Present=1",
    "Dash Button Polarity=0", "House Accept Charge Voltage=1380",
    "House Full Voltage=1420", "House High Voltage=1440",
    "Chassis Accept Charge Voltage=1240",
]
EXPECT = {"House Full Voltage": 1420, "House High Voltage": 1440}


def _settings_rows(c):
    return c.db.conn.execute(
        "SELECT ts, firmware, settings FROM acr_settings ORDER BY ts").fetchall()


def _events(c):
    return [r[0] for r in c.db.conn.execute(
        "SELECT type FROM events WHERE type LIKE 'acr_settings%' ORDER BY rowid")]


def test_parses_the_list_reply():
    fw, s = ble_mod.parse_acr_list([l + "\r" for l in LIST_6005] + ["OK", "ERROR",
                                                                     PARKED, "$LIST"])
    assert fw == "Lithionics ACR v6.0.05"
    assert s["House Full Voltage"] == 1420 and s["House High Voltage"] == 1440
    assert s["Version"] == 6005 and len(s) == 7


def test_reply_lines_are_collected_while_frames_keep_flowing(monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector()
    c._acr_list = []
    c._dispatch_acr(LIST_6005[0])
    c._dispatch_acr(PARKED)                 # a frame mid-reply is still a frame
    for line in LIST_6005[1:]:
        c._dispatch_acr(line)
    assert c._acr_list == LIST_6005
    assert [r[4] for r in _rows(c)] == [PARKED]
    lines, c._acr_list = c._acr_list, None
    c._store_acr_settings(lines)
    (ts, fw, raw), = _settings_rows(c)
    assert ts == 1000 and fw == "Lithionics ACR v6.0.05" and '"House High Voltage": 1440' in raw
    # Outside a reply the same line is just unrecognised.
    c._dispatch_acr("House Full Voltage=1420")
    assert c._acr_list is None and len(_settings_rows(c)) == 1


def test_a_change_between_reads_is_an_event(monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector()
    c._store_acr_settings(LIST_6005)
    clock.t = 2000
    c._store_acr_settings(LIST_6005)        # same again: no event
    assert _events(c) == []
    clock.t = 3000
    reset = [l.replace("=1440", "=1470") for l in LIST_6005]
    c._store_acr_settings(reset)
    assert _events(c) == ["acr_settings_changed"]
    detail = c.db.conn.execute(
        "SELECT detail FROM events WHERE type = 'acr_settings_changed'").fetchone()[0]
    assert json.loads(detail)["detail"]["changed"] == {"House High Voltage": [1440, 1470]}


def test_expected_settings_raise_once_and_clear(monkeypatch):
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector(acr_expect=EXPECT)
    c._store_acr_settings(LIST_6005)
    assert "acr_settings_unexpected" not in _events(c)
    reset = [l.replace("=1420", "=1450").replace("=1440", "=1470") for l in LIST_6005]
    for t in (2000, 3000):                  # two bad reads, one raise
        clock.t = t
        c._store_acr_settings(reset)
    clock.t = 4000
    c._store_acr_settings(LIST_6005)
    assert [e for e in _events(c) if e != "acr_settings_changed"] == [
        "acr_settings_unexpected", "acr_settings_unexpected_cleared"]


def test_unexpected_settings_are_a_finding(monkeypatch):
    from cerberus import findings
    clock = _Clock(1000)
    monkeypatch.setattr(ble_mod.time, "time", clock)
    c = _collector(acr_expect=EXPECT)
    c._store_acr_settings([l.replace("=1440", "=1470") for l in LIST_6005])
    out = [f for f in findings.derive(c.db.conn, findings.Settings(), 1100)
           if f.key == "acr_settings"]
    assert len(out) == 1 and out[0].status == findings.OPEN
    assert "House High Voltage 14.70 V, expected 14.40 V" in out[0].explain


def test_no_reply_stores_nothing(monkeypatch):
    c = _collector(acr_expect=EXPECT)
    c._store_acr_settings([])
    c._store_acr_settings(["ERROR"])
    assert _settings_rows(c) == [] and _events(c) == []


def test_list_is_sent_over_the_held_connection(monkeypatch):
    """The sidecar sends exactly `$LIST` on the ACR's own connection, and the
    reply lands in acr_settings. Nothing else is ever written."""
    monkeypatch.setattr(ble_mod, "ACR_LIST_FIRST_DELAY_S", 0.0)
    monkeypatch.setattr(ble_mod, "ACR_LIST_SETTLE_S", 0.05)
    writes = []

    class _Client:
        def __init__(self, addr, *a, **k):
            self.is_connected = True
            self._cb = None

        async def connect(self):
            pass

        async def disconnect(self):
            self.is_connected = False

        async def start_notify(self, _char, cb):
            self._cb = cb

        async def stop_notify(self, *a, **k):
            pass

        async def write_gatt_char(self, char, data, response=None):
            writes.append((char, bytes(data)))
            self._cb(None, bytearray(("\r\n".join(LIST_6005) + "\r\n").encode()))

    monkeypatch.setattr("cerberus.ble.BleakClient", _Client)
    c = _collector(acr_settings_seconds=3600)

    async def go():
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.3)
        c.shutdown()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert writes == [(ble_mod.FFE1_CHAR, b"$LIST\r\n")]
    assert len(_settings_rows(c)) == 1


def test_settings_reads_can_be_turned_off(monkeypatch):
    called = []

    class _Client:
        is_connected = True

        async def write_gatt_char(self, *a, **k):
            called.append(a)

    c = _collector(acr_settings_seconds=0)
    asyncio.run(c._acr_settings_loop(_Client()))
    assert called == []


def test_config_reads_acr_expect(tmp_path, monkeypatch):
    from cerberus import config as cfg_mod
    import pytest
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "x")
    src = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    plain = tmp_path / "p.yaml"
    plain.write_text(src)
    cfg = cfg_mod.load(plain)
    assert cfg.ble.acr_expect == {} and cfg.ble.acr_settings_seconds == 86400
    on = tmp_path / "on.yaml"
    on.write_text(src.replace("  # acr_expect:\n  #   House Full Voltage: 1420\n"
                              "  #   House High Voltage: 1440\n",
                              "  acr_expect:\n    House Full Voltage: 1420\n"
                              "    House High Voltage: 1440\n"))
    assert cfg_mod.load(on).ble.acr_expect == EXPECT
    bad = tmp_path / "bad.yaml"
    bad.write_text(on.read_text().replace("1420", "14.2 V"))
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod.load(bad)
