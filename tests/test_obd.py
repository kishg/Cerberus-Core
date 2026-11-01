"""ELM327 response parsing and PID decoding.

Exists because the question it serves cannot be answered by anything already
measured. Three drives ended with a full bank; 09-24 overshot to 14.75 V with
two CRIT alarms, 09-26 to 14.65 V for 46 minutes, and 09-28 stayed at exactly
14.40 V while pushing a *higher* +234.8 A. The breaker was on for both 09-26
and 09-28, so it is not the variable, and alternator current cannot be one
either — it cannot separate "engine running hard" from "engine running at all".

These tests run with no adapter plugged in, which is the point: the hardware
only answers from inside a van with the ignition on, so the parser has to be
verifiable without it.

Payload shapes below are the ELM327 wire format, not captures — there is no
adapter yet. Each expected value is computed from the published PID formula and
written out longhand in the test so a byte-order or scaling error cannot hide
behind a matching constant.
"""

from __future__ import annotations

from cerberus.obd import (
    PID_RPM,
    PID_SPEED,
    EngineReading,
    decode_coolant_c,
    decode_engine_load_pct,
    decode_rpm,
    decode_runtime_s,
    decode_speed_mph,
    payload_bytes,
    reading_from_replies,
)


def test_rpm_quarter_revolution_scaling():
    """RPM = ((A * 256) + B) / 4. 0x1AF8 = 6904 quarter-revs = 1726 rpm."""
    assert decode_rpm("410C1AF8") == (0x1A * 256 + 0xF8) / 4 == 1726.0


def test_rpm_at_a_plausible_idle():
    assert decode_rpm("410C0FA0") == 1000.0


def test_tolerates_echo_spaces_and_prompt():
    """ATE0 and ATS0 are sent at init, but an adapter that browned out mid-drive
    reconnects with its defaults restored and may answer in the verbose form
    before init runs again. Both must decode to the same number."""
    verbose = "010C\r41 0C 1A F8\r\r>"
    assert decode_rpm(verbose) == decode_rpm("410C1AF8") == 1726.0


def test_no_data_is_not_an_error():
    """The single most common reply this will ever see: the ignition is off.
    It must read as absence, not as a fault or a zero."""
    assert decode_rpm("NO DATA") is None
    assert payload_bytes(PID_RPM, "NO DATA\r\r>") is None


def test_searching_during_bus_negotiation():
    assert decode_rpm("SEARCHING...\r") is None


def test_zero_rpm_is_a_real_reading():
    """Ignition on, engine not turning. Distinct from NO DATA: the bus answered.
    0 is in range and must survive, because engine_running depends on telling
    'stopped' apart from 'not asked'."""
    r = decode_rpm("410C0000")
    assert r == 0.0
    assert EngineReading(rpm=0.0).engine_running is False
    assert EngineReading(rpm=800.0).engine_running is True


def test_out_of_range_rpm_is_dropped():
    """0xFFFF/4 = 16383.75, which no engine does. Bounds exist so a corrupted
    frame is dropped rather than stored — a bogus value silently poisons every
    correlation built on the table afterwards."""
    assert decode_rpm("410CFFFF") is None


def test_wrong_pid_in_reply_is_rejected():
    """Asked for RPM, got a speed reply. Without the PID check this would
    decode 0x50 as the high byte of an RPM and return a plausible 5120."""
    assert decode_rpm("410D50") is None


def test_truncated_reply_is_rejected():
    assert decode_rpm("410C1A") is None      # one data byte, needs two
    assert decode_rpm("410C1AF") is None     # odd nibble count


def test_coolant_is_offset_by_forty_celsius():
    assert decode_coolant_c("410550") == 80 - 40 == 40.0


def test_coolant_reads_below_zero():
    """Winter matters here: cold-soak charge lockout is the seasonal failure
    mode, so a sub-zero reading must not be clipped to 0."""
    assert decode_coolant_c("41051E") == 30 - 40 == -10.0


def test_speed_converts_kmh_to_mph():
    """The PID is km/h; stored as mph to match every other distance in the
    project notes. 80 km/h = 49.7 mph."""
    assert decode_speed_mph("410D50") == 49.7
    assert payload_bytes(PID_SPEED, "410D50") == [0x50]


def test_engine_load_scales_to_percent():
    assert decode_engine_load_pct("410480") == round(128 * 100 / 255, 1) == 50.2


def test_runtime_seconds():
    assert decode_runtime_s("411F0E10") == 0x0E * 256 + 0x10 == 3600


def test_partial_poll_cycle_still_yields_a_reading():
    """PIDs are polled individually and a vehicle may answer some and not
    others, so a partial reading is normal and worth storing."""
    r = reading_from_replies({
        "010C": "410C1AF8",
        "010D": "NO DATA",
        "0105": "410550",
    })
    assert r.rpm == 1726.0
    assert r.coolant_c == 40.0
    assert r.speed_mph is None
    assert r.is_empty is False


def test_all_no_data_is_empty():
    """Parked with the ignition off. is_empty is what stops the sidecar writing
    an all-null row, which would be indistinguishable from a decode failure."""
    r = reading_from_replies({pid: "NO DATA" for pid in ("010C", "010D", "0105")})
    assert r.is_empty is True
    assert r.engine_running is False


# --- sidecar loop shutdown behaviour ---------------------------------------
#
# These pin a bug found on 2026-09-30 while smoke-testing against a fabricated
# BLE address: BleakClient.connect() against an address CoreBluetooth cannot
# resolve never returns on macOS, and it does not honour bleak's own discovery
# timeout. The first version of _obd_loop used a bare `async with
# BleakClient(...)`, so a shutdown arriving during a connect attempt hung the
# sidecar indefinitely — launchd would have had to SIGKILL it.

import asyncio
import os
import tempfile

from cerberus.ble import BleCollector
from cerberus.config import BleConfig, ObdConfig
from cerberus.db import Db


def _cfgs(connect_timeout=0.2, idle_poll=0.1):
    ble = BleConfig(enabled=True, pack_addresses={}, cabin_sensors={},
                    cabin_sample_seconds=60, sample_downsample_seconds=30,
                    reconnect_backoff_seconds=1)
    obd = ObdConfig(enabled=True, address="unreachable",
                    write_char="w", notify_char="n",
                    poll_seconds=1, command_timeout_seconds=0.1,
                    init_timeout_seconds=0.1,
                    connect_timeout_seconds=connect_timeout,
                    reconnect_backoff_seconds=1,
                    parked_probe_seconds=idle_poll)
    return ble, obd


def _db():
    return Db(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_sleep_or_stop_wakes_immediately_on_shutdown():
    """The parked probe is 300 s, and parked is the common state. A bare
    asyncio.sleep there would hold shutdown for the full interval."""
    async def go():
        c = BleCollector(*_cfgs()[:1], db=_db())
        c.shutdown()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await c._sleep_or_stop(30.0)
        return loop.time() - t0
    assert asyncio.run(go()) < 1.0


def test_shutdown_is_not_blocked_by_a_hanging_connect(monkeypatch):
    """The regression. A connect that never returns must not outlive shutdown."""
    class HangingClient:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            await asyncio.Event().wait()      # never returns, like CoreBluetooth

        async def disconnect(self):
            pass

    monkeypatch.setattr("cerberus.ble.BleakClient", HangingClient)
    ble, obd = _cfgs()

    async def go():
        c = BleCollector(ble, _db(), obd_cfg=obd)
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.5)              # let it enter the connect
        loop = asyncio.get_running_loop()
        c.shutdown()
        t0 = loop.time()
        await asyncio.wait_for(task, timeout=5)
        return loop.time() - t0

    assert asyncio.run(go()) < 2.0


def test_engine_off_writes_no_rows(monkeypatch):
    """Connected but every PID answers NO DATA — the parked steady state. An
    all-null row would be indistinguishable from a decode failure and would
    accumulate across every parked day."""
    class NoDataClient:
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

        async def write_gatt_char(self, *a, **k):
            pass

    monkeypatch.setattr("cerberus.ble.BleakClient", NoDataClient)
    # Every ask() returns NO DATA, as the adapter does with the ignition off.
    monkeypatch.setattr("cerberus.ble._ElmChannel.ask",
                        lambda self, cmd, timeout: _immediate("NO DATA\r\r>"))
    ble, obd = _cfgs()
    db = _db()

    async def go():
        c = BleCollector(ble, db, obd_cfg=obd)
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.6)
        c.shutdown()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())
    assert db.conn.execute("select count(*) from engine_samples").fetchone()[0] == 0


async def _immediate(value):
    return value


# --- Engine-off parking -----------------------------------------------------
#
# The adapter stays plugged into the OBD port, which runs off the chassis
# battery. The first loop kept the connection open with the engine off and
# re-asked every PID every 30 s, which stops the adapter auto-sleeping and puts
# traffic on the vehicle bus. These pin the replacement: with the engine off,
# disconnect, and probe only with ATRV, which never reaches the bus.

from cerberus.obd import CMD_VOLTAGE, POLL_PLAN, decode_voltage


def test_voltage_plain():
    assert decode_voltage("12.6V\r\r>") == 12.6


def test_voltage_with_echo_left_on():
    assert decode_voltage("ATRV\r14.2V\r\r>") == 14.2


def test_voltage_unsupported_is_none():
    assert decode_voltage("?\r\r>") is None
    assert decode_voltage("") is None


def test_voltage_out_of_range_is_none():
    assert decode_voltage("0.4V") is None


class _ScriptedClient:
    """Connects instantly; counts connects and disconnects."""
    connects = 0
    disconnects = 0

    def __init__(self, *a, **k):
        self.is_connected = False

    async def connect(self):
        type(self).connects += 1
        self.is_connected = True

    async def disconnect(self):
        type(self).disconnects += 1
        self.is_connected = False

    async def start_notify(self, *a, **k):
        pass

    async def stop_notify(self, *a, **k):
        pass


def _run_scripted(monkeypatch, answer, seconds, script=None, acr_mode=None, **obd_over):
    """Run the OBD loop against answer(cmd) -> reply; return (cmds, db).
    script(collector) is an optional coroutine run alongside, to feed ACR
    modes the way _dispatch_acr would; acr_mode is the ACR's mode already
    heard when the loop starts."""
    cmds: list[str] = []

    async def ask(self, cmd, timeout):
        cmds.append(cmd)
        return answer(cmd)

    class Client(_ScriptedClient):
        connects = 0
        disconnects = 0

    monkeypatch.setattr("cerberus.ble.BleakClient", Client)
    monkeypatch.setattr("cerberus.ble._ElmChannel.ask", ask)
    ble, obd = _cfgs()
    obd = ObdConfig(**{**obd.__dict__, "poll_seconds": 0.01, **obd_over})
    db = _db()

    async def go():
        c = BleCollector(ble, db, obd_cfg=obd)
        if acr_mode is not None:
            c._note_acr_mode(acr_mode)
            c._engine_hint.clear()      # heard before the loop, not a fresh crank
        task = asyncio.create_task(c.run())
        side = asyncio.create_task(script(c)) if script else None
        await asyncio.sleep(seconds)
        c.shutdown()
        await asyncio.wait_for(task, timeout=5)
        if side:
            side.cancel()

    asyncio.run(go())
    return cmds, db, Client


RPM_1500 = "410C1770\r\r>"     # 0x1770 = 6000 quarter-revs = 1500 rpm


def test_parked_probe_never_touches_the_bus(monkeypatch):
    """Chassis at 12.6 V: only ATRV is ever sent — no ATZ, no PID — and the
    adapter is disconnected between probes so it can sleep."""
    cmds, db, Client = _run_scripted(
        monkeypatch, lambda cmd: "12.6V\r\r>", seconds=0.35,
        parked_probe_seconds=0.1,
    )
    assert cmds and set(cmds) == {CMD_VOLTAGE}
    assert Client.connects >= 2                     # it does come back to check
    assert Client.disconnects == Client.connects    # and lets go every time


def test_parked_probe_is_rate_limited(monkeypatch):
    """With the default 300 s probe nothing happens again within a second."""
    cmds, _, Client = _run_scripted(
        monkeypatch, lambda cmd: "12.6V\r\r>", seconds=0.5,
        parked_probe_seconds=300,
    )
    assert Client.connects == 1
    assert cmds == [CMD_VOLTAGE]


def test_running_voltage_starts_polling(monkeypatch):
    """Alternator charging (14.1 V) means init and poll, and rows land."""
    def answer(cmd):
        if cmd == CMD_VOLTAGE:
            return "14.1V\r\r>"
        if cmd == "010C":
            return RPM_1500
        return "OK\r\r>" if cmd.startswith("AT") else "NO DATA\r\r>"

    cmds, db, _ = _run_scripted(monkeypatch, answer, seconds=0.3)
    assert "ATZ" in cmds and "010C" in cmds
    rpms = [r[0] for r in db.conn.execute("select rpm from engine_samples")]
    assert rpms and set(rpms) == {1500.0}


def test_engine_stopping_parks_and_disconnects(monkeypatch):
    """Engine runs, then stops: after parked_after_empty_polls empty cycles
    the loop lets go and drops to ATRV-only probes."""
    state = {"cycles": 0}

    def answer(cmd):
        if cmd == CMD_VOLTAGE:
            # High on the first connect, resting thereafter.
            return "14.1V\r\r>" if state["cycles"] == 0 else "12.7V\r\r>"
        if cmd.startswith("AT"):
            return "OK\r\r>"
        if cmd == POLL_PLAN[0][0]:
            state["cycles"] += 1
        if cmd == "010C" and state["cycles"] <= 2:
            return RPM_1500
        return "NO DATA\r\r>"

    cmds, db, Client = _run_scripted(
        monkeypatch, answer, seconds=0.5,
        parked_after_empty_polls=3, parked_probe_seconds=0.1,
    )
    # >= 1, not 2: engine_samples is keyed on whole-second ts, so the two
    # running cycles 10 ms apart share a row here. At the real 2 s poll they
    # would not.
    assert db.conn.execute("select count(*) from engine_samples").fetchone()[0] >= 1
    # 2 running cycles + exactly 3 empty ones, then no more PID traffic.
    assert state["cycles"] == 5
    assert Client.connects >= 2
    last_pid = max(i for i, c in enumerate(cmds) if c.startswith("01"))
    assert cmds[last_pid + 1:] and set(cmds[last_pid + 1:]) == {CMD_VOLTAGE}


def test_one_empty_cycle_mid_drive_does_not_park(monkeypatch):
    """A single dropped cycle must not cost five minutes of RPM."""
    state = {"cycles": 0}

    def answer(cmd):
        if cmd == CMD_VOLTAGE:
            return "14.1V\r\r>"
        if cmd.startswith("AT"):
            return "OK\r\r>"
        if cmd == POLL_PLAN[0][0]:
            state["cycles"] += 1
        if cmd == "010C" and state["cycles"] != 2:
            return RPM_1500
        return "NO DATA\r\r>"

    _, db, Client = _run_scripted(
        monkeypatch, answer, seconds=0.3, parked_probe_seconds=300,
    )
    assert Client.connects == 1
    assert state["cycles"] > 3


def test_adapter_without_atrv_still_parks(monkeypatch):
    """'?' to ATRV: fall back to the PIDs, which park it after N empties."""
    def answer(cmd):
        if cmd == CMD_VOLTAGE:
            return "?\r\r>"
        return "OK\r\r>" if cmd.startswith("AT") else "NO DATA\r\r>"

    cmds, db, Client = _run_scripted(
        monkeypatch, answer, seconds=0.5, parked_probe_seconds=300,
    )
    assert Client.connects == 1 and Client.disconnects == 1
    assert sum(1 for c in cmds if c == POLL_PLAN[0][0]) == 3


# --- Broad capture -------------------------------------------------------------
#
# Since 2026-10-02 the goal is everything the van will give up, every drive,
# for a per-drive baseline. These pin the pieces: supported-PID discovery,
# config-driven Mercedes reads, raw-plus-decoded storage and DTC reads.

import pytest

from cerberus import config as cfg_mod
from cerberus.obd import (
    RequestSpec,
    FormulaError,
    build_plan,
    compile_formula,
    decode_dtcs,
    decode_spec,
    decode_supported,
)


def test_supported_bitmap_decodes_msb_first():
    # BE 1F A8 13: the bitmap a typical diesel ECU returns for 0100.
    got = decode_supported("0100", "4100BE1FA813\r\r>")
    assert {"0101", "0103", "0104", "0105", "0106", "0107"} <= got
    assert "0102" not in got
    assert "010C" in got and "010D" in got
    assert "0120" in got                        # last bit: next bitmap exists


def test_supported_bitmap_is_the_union_of_every_ecu():
    one = decode_supported("0100", "4100BE1FA813\r>")
    two = decode_supported("0100", "4100BE1FA813\r4100 80 00 00 00\r>")
    assert two == one | {"0101"}


# Real replies from this van's OBDLink CX (2026-11-01, engine idling): four
# ECUs answer the broadcast, and the first reply after ATSP0 carries
# "SEARCHING..." ahead of the data.
VAN_0100 = "SEARCHING...\r410098180001\r410098180001\r410098188013\r410098180001\r\r>"
VAN_0120 = "41208001A001\r412080018001\r41208001A001\r41208003A001\r\r>"
VAN_0140 = "414044C00001\r4140C0800000\r4140E4CC801D\r4140C4C00001\r\r>"


def test_searching_prefix_does_not_hide_the_bitmap():
    got = decode_supported("0100", VAN_0100)
    assert {"0101", "0104", "0105", "010C", "010D", "0120"} <= got
    assert {"0111", "011C", "011F"} <= got          # only in the engine ECU's line


def test_searching_prefix_does_not_hide_a_reading():
    assert decode_rpm("SEARCHING...\r410C0C00\r410C0C00\r\r>") == 768.0
    assert decode_rpm("SEARCHING...\rUNABLE TO CONNECT\r>") is None


def test_van_bitmaps_unlock_the_broad_catalogue():
    supported = set()
    for base, raw in (("0100", VAN_0100), ("0120", VAN_0120), ("0140", VAN_0140)):
        supported |= decode_supported(base, raw)
    names = {s.name for s in build_plan(supported)}
    assert {"module_voltage_v", "ambient_air_f", "fuel_level_pct", "oil_temp_c",
            "throttle_pct", "dtc_count"} <= names
    assert "maf_gps" not in names                   # 0110: NO DATA on this van


def test_formula_matches_published_pid_math():
    f = compile_formula("(A*256+B)/4")
    assert f([0x1A, 0xF8]) == (0x1A * 256 + 0xF8) / 4


def test_formula_rejects_anything_but_arithmetic():
    for bad in ("__import__('os')", "A.real", "max(A,B)", "Z+1", "A if B else C"):
        with pytest.raises(FormulaError):
            compile_formula(bad)


def test_formula_short_payload_is_none():
    assert compile_formula("A*256+B")([0x12]) is None


def test_service_22_reply_decodes():
    spec = RequestSpec(name="x", request="22F40D", unit="%", formula="A*100/255",
                       header="7E0")
    assert decode_spec(spec, "62F40DFF\r\r>") == 100.0
    assert decode_spec(spec, "7F2231\r\r>") is None     # negative response


def test_spec_range_limits_drop_garbage():
    spec = RequestSpec(name="x", request="0105", unit="C", formula="A-40",
                       min_value=-40, max_value=150)
    assert decode_spec(spec, "4105FF") is None           # 215 C


def test_dtcs_decode():
    # 2 codes: P0562 (system voltage low) and P2463 (DPF soot).
    assert decode_dtcs("43 02 05 62 24 63\r\r>") == ["P0562", "P2463"]


def test_no_dtcs():
    assert decode_dtcs("4300\r\r>") == []
    assert decode_dtcs("NO DATA\r\r>") == []
    assert decode_dtcs("") is None


def test_plan_falls_back_to_core_without_bitmaps():
    names = {s.name for s in build_plan(None)}
    assert names == {"rpm", "speed_mph", "coolant_c", "engine_load_pct", "runtime_s"}


def test_plan_filters_catalog_by_support_and_adds_extras():
    extra = (RequestSpec(name="alt", request="22AAAA", unit="%", formula="A",
                         header="7E0"),)
    plan = build_plan({"010C", "0142", "015E"}, extra)
    names = {s.name for s in plan}
    assert {"module_voltage_v", "fuel_rate_lph", "alt"} <= names
    assert "oil_temp_c" not in names
    assert "coolant_c" in names                 # core PIDs always polled


def _extra(**over):
    e = {"name": "alt_load", "request": "22aaaa", "unit": "%", "formula": "A"}
    e.update(over)
    return e


def test_config_extra_parses():
    (spec,) = cfg_mod._load_obd_extra([_extra(header="7e0", tier="slow")])
    assert spec.request == "22AAAA" and spec.header == "7E0" and spec.tier == "slow"


@pytest.mark.parametrize("bad", [
    _extra(formula="A+"),
    _extra(tier="sometimes"),
    _extra(request="22 AA"),
    _extra(header="7E"),
    _extra(name="rpm"),                      # collides with a standard PID
    {"name": "x", "request": "22AAAA"},      # missing unit/formula
])
def test_config_extra_rejects(bad):
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod._load_obd_extra([bad])


def test_session_stores_raw_and_decoded_and_reads_dtcs(monkeypatch):
    """End to end against a scripted adapter: bitmaps, DTCs, a standard PID
    beyond the core five, and a Mercedes read behind a header switch."""
    def answer(cmd):
        table = {
            CMD_VOLTAGE: "14.1V\r\r>",
            "0100": "4100 18 18 00 01\r\r>",   # 0104 0105 010C 010D 0120
            "0120": "4120 00 00 00 01\r\r>",   # only: 0140 exists
            "0140": "4140 40 00 00 00\r\r>",   # 0142
            "03": "43 01 05 62\r\r>",
            "07": "4700\r\r>",
            "010C": RPM_1500,
            "0142": "4142 37 0A\r\r>",         # 14.09 V
            "22AAAA": "62AAAA 80\r\r>",
        }
        if cmd in table:
            return table[cmd]
        return "OK\r\r>" if cmd.startswith("AT") else "NO DATA\r\r>"

    extra = (RequestSpec(name="alt_load", request="22AAAA", unit="%",
                         formula="A*100/255", tier="fast", header="7E0",
                         receive="7E8"),)
    cmds, db, _ = _run_scripted(monkeypatch, answer, seconds=0.3,
                                parked_probe_seconds=300, extra=extra)

    # Each bitmap's last bit says the next exists: 0100 -> 0120 -> 0140, and
    # 0140's is clear, so 0160 is never asked.
    assert "0140" in cmds and "0160" not in cmds
    got = dict(db.conn.execute(
        "SELECT name, value FROM obd_samples WHERE value IS NOT NULL"
    ).fetchall())
    assert got["rpm"] == 1500.0
    assert got["module_voltage_v"] == 14.09   # beyond the core five
    assert got["alt_load"] == round(0x80 * 100 / 255, 3)
    raw = db.conn.execute(
        "SELECT raw FROM obd_samples WHERE name = 'alt_load' LIMIT 1"
    ).fetchone()[0]
    assert "62AAAA" in raw.replace(" ", "")
    ecu = db.conn.execute(
        "SELECT ecu FROM obd_signals WHERE name = 'alt_load'"
    ).fetchone()[0]
    assert ecu == "7E0"
    dtc = dict(db.conn.execute("SELECT service, codes FROM obd_dtcs").fetchall())
    assert dtc == {"03": "P0562", "07": ""}
    # Header switched to the module for the extra read, then restored.
    i = cmds.index("ATSH7E0")
    assert cmds[i + 1] == "ATCRA7E8" and cmds[i + 2] == "22AAAA"
    assert "ATSH7DF" in cmds[i:] and "ATAR" in cmds[i:]
    assert "04" not in cmds                    # never clears codes


def test_def_level_from_the_vans_9b_reply():
    from cerberus.obd import STANDARD_CATALOG
    spec = next(s for s in STANDARD_CATALOG if s.name == "def_level_pct")
    assert round(decode_spec(spec, "419B0E7D38C8\r\r>"), 1) == 78.4    # 2026-11-01


# --- the ACR as the engine-on trigger (2026-11-01) --------------------------------
#
# With the engine off the ACR can join the house bank to the chassis (mode 3),
# so the chassis reads 13.3-14+ V and the voltage probe would call the engine
# running. A live ACR decides instead: modes 1/2/4 only happen with it running.

def _feed(modes):
    """A script that sets ACR modes at the given times: [(seconds, mode), ...]."""
    async def script(c):
        last = 0.0
        for at, mode in modes:
            await asyncio.sleep(at - last)
            last = at
            c._note_acr_mode(mode)
    return script


def _engine_answers(rpm_reply):
    def answer(cmd):
        if cmd == CMD_VOLTAGE:
            return "13.4V\r\r>"                  # house voltage on the chassis
        if cmd == "010C":
            return rpm_reply
        return "OK\r\r>" if cmd.startswith("AT") else "NO DATA\r\r>"
    return answer


def test_acr_off_mode_beats_a_high_chassis_voltage(monkeypatch):
    cmds, _, Client = _run_scripted(
        monkeypatch, _engine_answers("NO DATA\r\r>"), seconds=0.4,
        acr_mode=3, parked_probe_seconds=0.1)
    assert "ATZ" not in cmds and "010C" not in cmds    # never touched the bus
    assert Client.connects == 1                        # and stays disconnected


def test_acr_crank_wakes_obd_and_rpm_keeps_it_polling(monkeypatch):
    cmds, db, _ = _run_scripted(
        monkeypatch, _engine_answers(RPM_1500), seconds=0.5,
        acr_mode=0, script=_feed([(0.2, 1), (0.25, 2)]), parked_probe_seconds=300)
    assert "ATZ" in cmds and "010C" in cmds
    assert [r[0] for r in db.conn.execute("select rpm from engine_samples")][-1] == 1500.0


def test_engine_mode_without_rpm_does_not_reconnect_rapidly(monkeypatch):
    """ACR stuck in mode 2 but the engine says nothing: RPM has the last word,
    and the loop waits a full probe interval before trying again."""
    cmds, _, Client = _run_scripted(
        monkeypatch, _engine_answers("NO DATA\r\r>"), seconds=0.5,
        acr_mode=2, parked_probe_seconds=300)
    assert Client.connects == 1
