"""OBD-II engine data over a BLE ELM327-class adapter.

Why this exists
---------------
Three drives have now ended with a full bank. Two overshot the packs' own
14.4 V ``MaxChargeVoltage`` request and one did not:

    2026-09-24   14.75 V peak   13 alarm events in 5 min, two CRIT
    2026-09-26   14.65 V peak   46 min of BMS limit/release oscillation
    2026-09-28   14.40 V peak   clean, at +234.8 A — the highest ever recorded

The obvious explanations are both dead. The alternator disable breaker was
**on** for 09-26 and on for 09-28, so it is not the variable. Neither is time
spent full: the clean drive held >=99.5% SoC for 318 minutes against 57 on the
overshoot. What is left is *continuity* — 09-28 was short hops with the engine
off in between, letting SoC and voltage relax before each new burst, while
09-26 sat at 100% for 46 unbroken minutes with current reversing every sample.

That reading is consistent with the older RPM finding (absorption needs
sustained RPM at the *end* of a charge, not the start), and it cannot be
confirmed from anything currently measured. Alternator current is the confound,
not an independent variable: it cannot separate "engine running hard" from
"engine running at all". RPM can, and RPM has never been instrumented here.

The second question this answers is arguably worth more. 2026-09-24 was the
first time sustained draw was ever recorded on this rig — 190-220 A for most of
an hour, where all previous data held was one 60 s and one 12 s event. Whether
that load is being asked of the alternator at *low* RPM is its thermally worst
case, and the stock internally-regulated MB unit publishes nothing about
itself. Coolant temperature and engine load come free on the same connection.

Scope
-----
Read-only, Mode 01 (current data) only. Nothing here writes to the vehicle bus.
No Mode 03/04 (read/clear DTCs) and no manufacturer-specific PIDs: the goal is
a tachometer trace next to the charge trace, not diagnostics.

Transport
---------
This module is deliberately transport-free. It owns the ELM327 line protocol
and the PID decoders as pure functions; ``cerberus.ble`` supplies an ``ask``
coroutine backed by BleakClient, the same way ``govee.py`` owns decoding while
``ble.py`` owns the radio. That split is what makes the parser testable with no
adapter plugged in, which matters because the adapter only works in a van with
the ignition on.

ELM327 line protocol
--------------------
Commands are ASCII terminated with CR. The adapter replies with the data, then
a ``>`` prompt meaning "ready for the next command"::

    >010C\r          request engine RPM
    41 0C 1A F8      mode 41 = response to 01, PID 0C, two data bytes
    >

``ATE0`` turns off command echo and ``ATS0`` strips the spaces, so in practice
the reply is a bare ``410C1AF8``. Both are sent at init anyway and the parser
tolerates either form, because a adapter that has been power-cycled mid-session
comes back with its defaults restored.

Non-answers are normal and must not be treated as faults. ``NO DATA`` is what a
PID returns with the ignition off, which on this rig is most of the time;
``SEARCHING...`` appears while the adapter negotiates the bus on first use.
"""

from __future__ import annotations

import ast as _ast
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("cerberus.obd")

# Nordic UART Service. The most common BLE-serial profile and the right first
# guess, but it is only a guess: the packs on this rig use FFE0/FFE1 instead,
# and adapters differ. These are config-overridable for exactly that reason —
# rediscover with `python tools/ble_capture.py scan` if writes go nowhere.
DEFAULT_WRITE_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_NOTIFY_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Sent once per connection, in order.
#   ATZ   full reset — slowest command there is, hence the longer init timeout
#   ATE0  echo off, so replies are not prefixed with the command
#   ATL0  no linefeeds; CR alone terminates
#   ATS0  strip spaces from responses
#   ATH0  no CAN headers, we only want the payload
#   ATSP0 auto-detect protocol (the Sprinter is CAN, but let the adapter prove it)
INIT_COMMANDS: tuple[str, ...] = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")

PID_ENGINE_LOAD = "0104"
PID_COOLANT = "0105"
PID_RPM = "010C"
PID_SPEED = "010D"
PID_RUNTIME = "011F"

# Everything the adapter says that is not hex payload. Checked as substrings
# against the upper-cased reply, so partial lines still match.
_NON_DATA = (
    "NO DATA",          # ignition off, or PID unsupported — the common case
    "SEARCHING",        # protocol negotiation, first request after connect
    "STOPPED",
    "UNABLE TO CONNECT",
    "CAN ERROR",
    "BUS INIT",
    "BUFFER FULL",
    "ERROR",
)

_HEX_ONLY = re.compile(r"[^0-9A-Fa-f]")

# Sanity bounds, with the same intent as govee.decode's: a corrupted frame must
# be dropped rather than stored, because a bogus value silently poisons every
# correlation built on this table afterwards.
#
# RPM 8000 is far above where a 3.0 L OM642 diesel will ever be (redline ~4500);
# it is set to catch byte-order and scaling errors, not to model the engine.
_RPM_MAX = 8000.0
_COOLANT_MIN_C, _COOLANT_MAX_C = -40.0, 150.0
_SPEED_MAX_MPH = 120.0
_RUNTIME_MAX_S = 65535          # the PID is 16-bit and wraps at ~18 h


@dataclass(frozen=True)
class EngineReading:
    """One poll cycle. Every field is optional: PIDs are polled individually
    and a vehicle may answer some and not others, so a partial reading is
    normal and still worth storing."""
    rpm: float | None = None
    coolant_c: float | None = None
    speed_mph: float | None = None
    engine_load_pct: float | None = None
    runtime_s: int | None = None

    @property
    def engine_running(self) -> bool:
        """True when the engine is turning.

        Deliberately RPM-based rather than trusting ``runtime_s``: run time
        keeps its last value briefly after shutdown on some ECUs, and the whole
        point of this module is to know precisely *when* the alternator stops.
        """
        return self.rpm is not None and self.rpm > 0

    @property
    def is_empty(self) -> bool:
        return all(
            v is None for v in
            (self.rpm, self.coolant_c, self.speed_mph,
             self.engine_load_pct, self.runtime_s)
        )


def response_prefix(request: str) -> str:
    """The hex a positive reply to ``request`` starts with."""
    req = request.upper()
    return f"{int(req[:2], 16) + 0x40:02X}{req[2:]}"


def payload_bytes(pid: str, raw: str) -> list[int] | None:
    """Extract the data bytes of a Mode 01 reply, or None if it is not one.

    Returns None rather than raising for every unexpected input — ``NO DATA``
    with the ignition off is the single most common reply this will ever see,
    and it is not an error condition.

    Tolerates echo left on, spaces left in, and multi-line replies, because an
    adapter that browned out mid-drive reconnects with its defaults restored
    and the init sequence may not have run yet.
    """
    if not raw:
        return None
    upper = raw.upper()
    if any(marker in upper for marker in _NON_DATA):
        return None

    # Strip everything that is not hex, which also disposes of the '>' prompt,
    # CR/LF, spaces and any echoed command prefix's punctuation.
    flat = _HEX_ONLY.sub("", upper)

    # The echo, if present, is the request itself: drop a leading copy of it.
    if flat.startswith(pid.upper()):
        flat = flat[len(pid):]

    # A positive reply is the service byte + 0x40 followed by the rest of the
    # request echoed back: "010C" -> "410C", "22F190" -> "62F190", "03" -> "43".
    expect = response_prefix(pid)
    idx = flat.find(expect)
    if idx < 0:
        return None
    data = flat[idx + len(expect):]
    if len(data) < 2 or len(data) % 2:
        return None
    try:
        return [int(data[i:i + 2], 16) for i in range(0, len(data), 2)]
    except ValueError:
        return None


def decode_rpm(raw: str) -> float | None:
    """RPM = ((A * 256) + B) / 4 — the PID carries quarter-revolutions."""
    b = payload_bytes(PID_RPM, raw)
    if b is None or len(b) < 2:
        return None
    rpm = ((b[0] * 256) + b[1]) / 4.0
    if not (0.0 <= rpm <= _RPM_MAX):
        return None
    return round(rpm, 1)


def decode_coolant_c(raw: str) -> float | None:
    """Coolant temperature = A - 40 C. Celsius, per the project convention that
    equipment is C and only ambient air is F."""
    b = payload_bytes(PID_COOLANT, raw)
    if b is None or not b:
        return None
    temp_c = float(b[0] - 40)
    if not (_COOLANT_MIN_C <= temp_c <= _COOLANT_MAX_C):
        return None
    return temp_c


def decode_speed_mph(raw: str) -> float | None:
    """Vehicle speed. The PID is km/h; stored as mph because every other
    distance/speed figure in this project's notes is imperial."""
    b = payload_bytes(PID_SPEED, raw)
    if b is None or not b:
        return None
    mph = b[0] * 0.621371
    if not (0.0 <= mph <= _SPEED_MAX_MPH):
        return None
    return round(mph, 1)


def decode_engine_load_pct(raw: str) -> float | None:
    """Calculated engine load = A * 100 / 255."""
    b = payload_bytes(PID_ENGINE_LOAD, raw)
    if b is None or not b:
        return None
    pct = b[0] * 100.0 / 255.0
    if not (0.0 <= pct <= 100.0):
        return None
    return round(pct, 1)


def decode_runtime_s(raw: str) -> int | None:
    """Run time since engine start = (A * 256) + B seconds."""
    b = payload_bytes(PID_RUNTIME, raw)
    if b is None or len(b) < 2:
        return None
    secs = (b[0] * 256) + b[1]
    if not (0 <= secs <= _RUNTIME_MAX_S):
        return None
    return secs


# PID -> (attribute name, decoder). Polled in this order; RPM first so that the
# most important signal is captured even if a later PID stalls the cycle.
POLL_PLAN: tuple[tuple[str, str], ...] = (
    (PID_RPM, "rpm"),
    (PID_SPEED, "speed_mph"),
    (PID_COOLANT, "coolant_c"),
    (PID_ENGINE_LOAD, "engine_load_pct"),
    (PID_RUNTIME, "runtime_s"),
)

_DECODERS = {
    PID_RPM: decode_rpm,
    PID_SPEED: decode_speed_mph,
    PID_COOLANT: decode_coolant_c,
    PID_ENGINE_LOAD: decode_engine_load_pct,
    PID_RUNTIME: decode_runtime_s,
}


def reading_from_replies(replies: dict[str, str]) -> EngineReading:
    """Build an EngineReading from a {pid: raw_reply} map.

    Pure, so the whole poll cycle can be tested against captured adapter
    output with no adapter present.
    """
    fields: dict[str, object] = {}
    for pid, attr in POLL_PLAN:
        raw = replies.get(pid)
        if raw is None:
            continue
        value = _DECODERS[pid](raw)
        if value is not None:
            fields[attr] = value
    return EngineReading(**fields)  # type: ignore[arg-type]


# ATRV asks the adapter for the voltage on OBD pin 16, which is the chassis
# battery. The adapter answers this itself from its own ADC: nothing goes onto
# the vehicle bus, so it is the one question that can be asked of a parked van
# without waking its ECUs. A running alternator holds the chassis battery
# around 13.8-14.4 V; a resting one sits at 12.4-12.9 V.
CMD_VOLTAGE = "ATRV"

_VOLTS = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*V", re.IGNORECASE)
_VOLTS_MIN, _VOLTS_MAX = 5.0, 30.0


def decode_voltage(raw: str) -> float | None:
    """Parse an ATRV reply such as ``12.6V``, or None.

    Tolerates the echoed command (``ATRV`` carries no digits before its V, so
    it cannot match) and the trailing prompt. None for ``?`` from an adapter
    that does not implement ATRV, which the caller treats as "unknown" and
    falls back to asking the engine directly.
    """
    if not raw:
        return None
    m = _VOLTS.search(raw)
    if m is None:
        return None
    volts = float(m.group(1))
    if not (_VOLTS_MIN <= volts <= _VOLTS_MAX):
        return None
    return volts


# ---------------------------------------------------------------------------
# Broad capture
# ---------------------------------------------------------------------------
#
# The five PIDs above answer one question (what was RPM doing when the voltage
# overshot). The goal since 2026-10-02 is wider: log everything the van will
# give up, every drive, so that a per-drive baseline can be built and drift
# measured against it. Two sources feed that:
#
#   * STANDARD_CATALOG — SAE J1979 Mode 01 PIDs, filtered at connect time to
#     what this ECU says it supports (the 0100/0120/... bitmaps). Nothing here
#     is van-specific.
#   * RequestSpec entries from config (obd.extra) — Mercedes UDS reads
#     (service 22) addressed to a specific module, discovered by sniffing the
#     NT510 through a Y-splitter. Config, not code, so a newly decoded value is
#     one YAML entry.
#
# Every reply is stored raw alongside its decoded value (obd_samples). A
# formula that turns out wrong — likely for anything reverse-engineered — can
# then be re-run over history instead of the history being lost with it.

TIERS = ("fast", "medium", "slow")

# Poll cycles between reads, at the default 2 s poll: fast 2 s, medium 10 s,
# slow 60 s. Bluetooth ELM327s manage on the order of 10-20 requests a second,
# so the fast tier has to stay short — the cycle time is logged at session
# start so this can be tuned against the real adapter.
TIER_EVERY = {"fast": 1, "medium": 5, "slow": 30}

SUPPORTED_BITMAP_PIDS = ("0100", "0120", "0140", "0160", "0180", "01A0")


@dataclass(frozen=True)
class RequestSpec:
    """One thing to ask the van, and how to read the answer.

    ``formula`` is an arithmetic expression over the payload bytes A, B, C,
    D... (the bytes after the echoed request), the notation every PID table and
    most reverse-engineering notes already use. ``header`` is None for standard
    OBD (functional 7DF broadcast); a Mercedes module read sets it to the
    module's request ID, e.g. "7E0" for the engine ECU.
    """
    name: str
    request: str
    unit: str
    formula: str
    tier: str = "medium"
    header: str | None = None
    receive: str | None = None
    min_value: float | None = None
    max_value: float | None = None

    @property
    def ecu(self) -> str:
        return self.header or "7DF"


def _std(pid, name, unit, formula, tier, lo=None, hi=None) -> RequestSpec:
    return RequestSpec(name=name, request=pid, unit=unit, formula=formula,
                       tier=tier, min_value=lo, max_value=hi)


# Units follow the project convention: equipment temperatures in C, ambient air
# in F, distances and speeds imperial. The km/h and km PIDs are converted in
# the formula so the stored value is already in the unit its name says.
STANDARD_CATALOG: tuple[RequestSpec, ...] = (
    # fast — what the engine is doing right now
    _std("010C", "rpm", "rpm", "(A*256+B)/4", "fast", 0, 8000),
    _std("010D", "speed_mph", "mph", "A*0.621371", "fast", 0, 120),
    _std("0104", "engine_load_pct", "%", "A*100/255", "fast", 0, 100),
    _std("0111", "throttle_pct", "%", "A*100/255", "fast", 0, 100),
    _std("0149", "accel_pedal_d_pct", "%", "A*100/255", "fast", 0, 100),
    _std("010B", "intake_map_kpa", "kPa", "A", "fast", 0, 255),
    _std("0110", "maf_gps", "g/s", "(A*256+B)/100", "fast", 0, 700),
    _std("015E", "fuel_rate_lph", "L/h", "(A*256+B)/20", "fast", 0, 100),
    _std("0162", "actual_torque_pct", "%", "A-125", "fast", -125, 130),
    # medium — temperatures and voltages, which move over minutes
    # Coolant and runtime are slow-moving, but they are two of the five core
    # PIDs behind engine_samples and the HA entities, which are written every
    # cycle — on a slower tier 4 rows in 5 would carry a null coolant.
    _std("0105", "coolant_c", "C", "A-40", "fast", -40, 150),
    _std("011F", "runtime_s", "s", "A*256+B", "fast", 0, 65535),
    _std("015C", "oil_temp_c", "C", "A-40", "medium", -40, 170),
    _std("010F", "intake_air_c", "C", "A-40", "medium", -40, 120),
    _std("0146", "ambient_air_f", "F", "(A-40)*9/5+32", "medium", -40, 140),
    _std("0142", "module_voltage_v", "V", "(A*256+B)/1000", "medium", 5, 30),
    _std("0133", "baro_kpa", "kPa", "A", "medium", 50, 120),
    _std("0161", "demand_torque_pct", "%", "A-125", "medium", -125, 130),
    # slow — counters and levels
    _std("012F", "fuel_level_pct", "%", "A*100/255", "slow", 0, 100),
    _std("0163", "reference_torque_nm", "Nm", "A*256+B", "slow", 0, 65535),
    _std("0101", "dtc_count", "count", "A % 128", "slow", 0, 127),
    _std("0121", "miles_with_mil", "mi", "(A*256+B)*0.621371", "slow"),
    _std("0131", "miles_since_clear", "mi", "(A*256+B)*0.621371", "slow"),
    _std("0130", "warmups_since_clear", "count", "A", "slow"),
    _std("01A6", "odometer_mi", "mi", "(A*16777216+B*65536+C*256+D)/10*0.621371", "slow"),
)


def decode_supported(base: str, raw: str) -> set[str]:
    """Decode a supported-PIDs bitmap reply (0100, 0120, ...).

    Four bytes, MSB first; bit n set means PID base+1+n is supported. Only the
    first four bytes are read, because a second ECU answering the broadcast
    appends its own bitmap to the same line.
    """
    b = payload_bytes(base, raw)
    if b is None or len(b) < 4:
        return set()
    start = int(base[2:], 16)
    out: set[str] = set()
    bits = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
    for n in range(32):
        if bits & (1 << (31 - n)):
            out.add(f"01{start + 1 + n:02X}")
    return out


# --- formula evaluation ----------------------------------------------------
#
# Formulas come from config, so they are parsed rather than eval()'d: numbers,
# the byte names A-H, + - * / // % and parentheses, nothing else.

_BYTE_NAMES = "ABCDEFGH"
_OPS = {
    _ast.Add: lambda a, b: a + b,
    _ast.Sub: lambda a, b: a - b,
    _ast.Mult: lambda a, b: a * b,
    _ast.Div: lambda a, b: a / b,
    _ast.FloorDiv: lambda a, b: a // b,
    _ast.Mod: lambda a, b: a % b,
}


class FormulaError(ValueError):
    pass


def compile_formula(formula: str):
    """Validate ``formula`` and return a callable taking the byte list.

    Raises FormulaError at config load for anything outside the grammar, so a
    typo fails at startup rather than silently yielding None every drive.
    """
    try:
        tree = _ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"bad formula {formula!r}: {e}") from None
    used: set[int] = set()

    def check(node):
        if isinstance(node, _ast.Expression):
            check(node.body)
        elif isinstance(node, _ast.BinOp) and type(node.op) in _OPS:
            check(node.left)
            check(node.right)
        elif isinstance(node, _ast.UnaryOp) and isinstance(node.op, _ast.USub):
            check(node.operand)
        elif isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
            pass
        elif isinstance(node, _ast.Name) and node.id in _BYTE_NAMES:
            used.add(_BYTE_NAMES.index(node.id))
        else:
            raise FormulaError(
                f"formula {formula!r}: only numbers, A-H and + - * / // % are allowed"
            )

    check(tree)
    need = max(used) + 1 if used else 0

    def ev(node, b):
        if isinstance(node, _ast.Expression):
            return ev(node.body, b)
        if isinstance(node, _ast.BinOp):
            return _OPS[type(node.op)](ev(node.left, b), ev(node.right, b))
        if isinstance(node, _ast.UnaryOp):
            return -ev(node.operand, b)
        if isinstance(node, _ast.Constant):
            return node.value
        return b[_BYTE_NAMES.index(node.id)]

    def run(b: list[int]) -> float | None:
        if len(b) < need:
            return None
        try:
            return float(ev(tree, b))
        except ZeroDivisionError:
            return None

    return run


def decode_spec(spec: RequestSpec, raw: str, fn=None) -> float | None:
    """Decoded value for one reply, or None (no answer, short, out of range)."""
    b = payload_bytes(spec.request, raw)
    if b is None:
        return None
    value = (fn or compile_formula(spec.formula))(b)
    if value is None:
        return None
    if spec.min_value is not None and value < spec.min_value:
        return None
    if spec.max_value is not None and value > spec.max_value:
        return None
    return round(value, 3)


def decode_dtcs(raw: str, service: str = "03") -> list[str] | None:
    """Stored (03) or pending (07) trouble codes, e.g. ["P0562"], or None.

    Read-only. On CAN the reply is 43 NN then two bytes per code; NN is the
    count. An empty list is a real answer ("no codes"), None is no answer.
    Multi-frame replies from a busy ECU may not survive the ATH0 flattening,
    which is why the raw reply is stored next to this.
    """
    b = payload_bytes(service, raw)
    if b is None:
        if raw and "NO DATA" in raw.upper():
            return []
        return None
    if not b:
        return []
    count, data = b[0], b[1:]
    codes: list[str] = []
    for i in range(0, min(len(data), count * 2) - 1, 2):
        hi, lo = data[i], data[i + 1]
        if hi == 0 and lo == 0:
            continue
        letter = "PCBU"[hi >> 6]
        codes.append(f"{letter}{(hi >> 4) & 0x3}{hi & 0xF:X}{lo:02X}")
    return codes


def build_plan(supported: set[str] | None,
               extra: tuple[RequestSpec, ...] = ()) -> tuple[RequestSpec, ...]:
    """Everything to poll this session: supported standard PIDs plus config
    extras. With no bitmap answer (supported None) fall back to the original
    five, which every OBD-II diesel answers."""
    core = {pid for pid, _ in POLL_PLAN}
    if supported is None:
        std = tuple(s for s in STANDARD_CATALOG if s.request in core)
    else:
        std = tuple(s for s in STANDARD_CATALOG
                    if s.request in supported or s.request in core)
    return std + tuple(extra)
