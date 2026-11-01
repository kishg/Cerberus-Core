"""AC-side state from the Cerbo's system and vebus topics.

Cerberus was battery-only until 2026-08-08: it subscribed to the DC tree and
nothing else, so an AC load was visible only as its DC reflection through the
inverter and never as an actual watt figure. This module holds the AC picture.

Three sources, deliberately kept distinct because they answer different
questions:

  system/0/Ac/Consumption/L<n>/{Power,Current}  — what the loads are drawing
  system/0/Ac/ActiveIn/{Connected,Source} +
  vebus/<inst>/Ac/ActiveIn/L<n>/{P,I,V,F}       — what shore/generator supplies
  vebus/<inst>/Ac/Out/L<n>/{P,I,V,F}            — what the inverter puts out

plus the inverter's own DC side, vebus/<inst>/Dc/0/{Voltage,Current}: the
voltage at the inverter's battery terminals, which is what its LowBattery
alarm acts on.

On a single-phase rig (this van) only L1 carries values; L2/L3 publish null and
are skipped. Sums are over whatever phases actually reported, so the same code
works if the rig ever goes three-phase.

Values arrive one topic at a time, each as its own MQTT message, so this is a
running accumulator rather than a parse of one payload. Nothing here is
persisted or published directly — callers read the properties.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# VE.Bus inverter/charger state. Numeric on the wire; these are the labels the
# Victron remote console shows, so they match what the user sees on the Cerbo.
VEBUS_STATE = {
    0: "off",
    1: "low power",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize",
    8: "passthru",
    9: "inverting",
    10: "power assist",
    11: "power supply",
    252: "bulk protection",
}

# Charging states — used to tell "the inverter is running loads off the
# battery" from "shore power is replenishing the bank".
CHARGING_STATES = frozenset({3, 4, 5, 7, 11, 252})

_PREFIX = re.compile(r"^N/[^/]+/(?P<rest>.+)$")

_CONSUMPTION = re.compile(r"^system/0/Ac/Consumption/L(?P<phase>\d)/(?P<field>Power|Current)$")
_SYS_ACTIVE_IN = re.compile(r"^system/0/Ac/ActiveIn/L(?P<phase>\d)/(?P<field>Power|Current)$")
_SYS_SOURCE = re.compile(r"^system/0/Ac/ActiveIn/Source$")
# Shore/generator presence. Verified against a full topic dump on 2026-08-08:
# there is NO system/0/Ac/ActiveIn/Connected on this firmware — the real
# signals are the per-input system/0/Ac/In/<idx>/Connected and the vebus
# ActiveIn/Connected. An earlier guess at the ActiveIn path silently never
# matched, leaving shore state permanently unknown.
_SYS_AC_IN = re.compile(r"^system/0/Ac/In/(?P<idx>\d+)/(?P<field>Connected|Source)$")
_VEBUS_CONNECTED = re.compile(r"^vebus/(?P<inst>\d+)/Ac/ActiveIn/Connected$")
_VEBUS_OUT = re.compile(r"^vebus/(?P<inst>\d+)/Ac/Out/L(?P<phase>\d)/(?P<field>[PIVF])$")
_VEBUS_IN = re.compile(r"^vebus/(?P<inst>\d+)/Ac/ActiveIn/L(?P<phase>\d)/(?P<field>[PIVF])$")
_VEBUS_IN_TOTAL = re.compile(r"^vebus/(?P<inst>\d+)/Ac/ActiveIn/P$")
_VEBUS_STATE = re.compile(r"^vebus/(?P<inst>\d+)/State$")
_VEBUS_MODE = re.compile(r"^vebus/(?P<inst>\d+)/Mode$")
_VEBUS_DC = re.compile(r"^vebus/(?P<inst>\d+)/Dc/0/(?P<field>Voltage|Current)$")


def _num(value) -> float | None:
    """Victron publishes null for anything not applicable (absent phase, no
    shore power). Treat null and non-numerics as 'no reading', not as zero —
    zero is a real measurement and must stay distinguishable from missing."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass
class AcState:
    """Running accumulator of the latest value seen on each AC topic."""

    consumption_w: dict[int, float] = field(default_factory=dict)
    consumption_a: dict[int, float] = field(default_factory=dict)
    input_w: dict[int, float] = field(default_factory=dict)
    input_a: dict[int, float] = field(default_factory=dict)
    output_w: dict[int, float] = field(default_factory=dict)
    output_v: dict[int, float] = field(default_factory=dict)
    input_total_w: float | None = None
    # Per-AC-input connected flags (system/0/Ac/In/<idx>/Connected), plus the
    # vebus view as a fallback for firmwares that only expose that one.
    inputs_connected: dict[int, bool] = field(default_factory=dict)
    vebus_input_connected: bool | None = None
    input_source: int | None = None
    inverter_state: int | None = None
    inverter_mode: int | None = None
    # Inverter DC terminals. Current is as Victron signs it: negative while
    # inverting (drawing from the bank). Both go null with the inverter off.
    dc_voltage: float | None = None
    dc_current: float | None = None
    last_update_ts: int = 0

    def update(self, topic: str, value) -> bool:
        """Fold one message in. Returns True if it was an AC topic we track."""
        m = _PREFIX.match(topic)
        if not m:
            return False
        rest = m.group("rest")

        if (mm := _CONSUMPTION.match(rest)):
            target = self.consumption_w if mm.group("field") == "Power" else self.consumption_a
            return self._put(target, int(mm.group("phase")), value)
        if (mm := _SYS_ACTIVE_IN.match(rest)):
            target = self.input_w if mm.group("field") == "Power" else self.input_a
            return self._put(target, int(mm.group("phase")), value)
        if (mm := _VEBUS_OUT.match(rest)):
            f = mm.group("field")
            if f == "P":
                return self._put(self.output_w, int(mm.group("phase")), value)
            if f == "V":
                return self._put(self.output_v, int(mm.group("phase")), value)
            return True  # tracked topic, value we don't surface
        if (mm := _VEBUS_IN.match(rest)):
            f = mm.group("field")
            if f == "P":
                return self._put(self.input_w, int(mm.group("phase")), value)
            if f == "I":
                return self._put(self.input_a, int(mm.group("phase")), value)
            return True
        if _VEBUS_IN_TOTAL.match(rest):
            # Null clears, exactly as in _put. This field is not just cached,
            # it is *preferred* by total_input_w over the per-phase sum, so a
            # latched value here outranks the phases even after they correctly
            # drop out. That is what produced rows reading `input_w = 0.0`
            # for 15 hours after shore was unplugged on 2026-09-17: the phases
            # had cleared, but the last total stuck and kept winning.
            self.input_total_w = _num(value)
            self._stamp()
            return True
        if (mm := _SYS_AC_IN.match(rest)):
            v = _num(value)
            idx = int(mm.group("idx"))
            if mm.group("field") == "Connected":
                # null means "this AC input doesn't exist on this rig" — drop
                # it so it can't count toward the any() below.
                if v is None:
                    self.inputs_connected.pop(idx, None)
                else:
                    self.inputs_connected[idx] = bool(v)
            elif v is not None:
                self.input_source = int(v)
            self._stamp()
            return True
        if _VEBUS_CONNECTED.match(rest):
            v = _num(value)
            if v is not None:
                self.vebus_input_connected = bool(v)
                self._stamp()
            return True
        if _SYS_SOURCE.match(rest):
            v = _num(value)
            if v is not None:
                self.input_source = int(v)
                self._stamp()
            return True
        if (mm := _VEBUS_DC.match(rest)):
            # Null clears: a switched-off inverter must not leave its last
            # terminal voltage latched into every later row.
            if mm.group("field") == "Voltage":
                self.dc_voltage = _num(value)
            else:
                self.dc_current = _num(value)
            self._stamp()
            return True
        if _VEBUS_STATE.match(rest):
            v = _num(value)
            if v is not None:
                self.inverter_state = int(v)
                self._stamp()
            return True
        if _VEBUS_MODE.match(rest):
            v = _num(value)
            if v is not None:
                self.inverter_mode = int(v)
                self._stamp()
            return True
        return False

    def _put(self, target: dict[int, float], phase: int, value) -> bool:
        v = _num(value)
        if v is None:
            # A phase that goes null (shore unplugged) must stop contributing
            # to the sums, otherwise the last live reading sticks forever.
            target.pop(phase, None)
        else:
            target[phase] = v
        self._stamp()
        return True

    def _stamp(self) -> None:
        self.last_update_ts = int(time.time())

    # -- derived ------------------------------------------------------------

    @staticmethod
    def _sum(d: dict[int, float]) -> float | None:
        return sum(d.values()) if d else None

    @property
    def total_consumption_w(self) -> float | None:
        """AC load across all reporting phases — the number that answers
        'what are we actually running right now'."""
        return self._sum(self.consumption_w)

    @property
    def total_input_w(self) -> float | None:
        """Shore/generator input. Prefers the vebus total when present since
        it is measured rather than summed."""
        if self.input_total_w is not None:
            return self.input_total_w
        return self._sum(self.input_w)

    @property
    def total_output_w(self) -> float | None:
        """Inverter AC output."""
        return self._sum(self.output_w)

    @property
    def shore_connected(self) -> bool | None:
        """True if any AC input reports connected.

        Prefers the system-level per-input flags; falls back to the vebus
        ActiveIn flag. None when neither has been seen yet — which must stay
        distinct from False, since "we don't know" and "definitely unplugged"
        lead to different conclusions about an overnight discharge.
        """
        if self.inputs_connected:
            return any(self.inputs_connected.values())
        return self.vebus_input_connected

    @property
    def inverter_state_label(self) -> str | None:
        if self.inverter_state is None:
            return None
        return VEBUS_STATE.get(self.inverter_state, f"unknown ({self.inverter_state})")

    @property
    def is_charging_from_ac(self) -> bool:
        """True when shore/generator is connected AND the inverter/charger is
        in a charging state — i.e. the bank is being replenished from AC."""
        return bool(self.shore_connected) and self.inverter_state in CHARGING_STATES

    @property
    def has_data(self) -> bool:
        return self.last_update_ts > 0
