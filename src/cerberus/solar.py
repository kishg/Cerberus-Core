"""Solar charge-controller state from the Cerbo's solarcharger topics.

Added 2026-08-09. Until now Cerberus recorded battery and AC but nothing about
where a charge came from, so "why was solar low today" could only be answered
from a live reading — there was no history to plot. This module fixes that.

Two views, both worth keeping:

  solarcharger/<inst>/Pv/V, Yield/Power     what the array is producing
  solarcharger/<inst>/Dc/0/{Voltage,Current} what reaches the battery
  solarcharger/<inst>/State, ErrorCode       why it might not be producing more
  solarcharger/<inst>/History/Daily/0/*      today's running total and peak
  system/0/Dc/Pv/Power                       system-wide PV, all controllers

The State field matters as much as the power figure. A controller in Float is
throttling on purpose because the bank is full, which looks identical to a
shaded array if you only log watts. Recording the state is what separates
"not much sun" from "nowhere to put it".

Values arrive one topic per message, so this is a running accumulator. Written
to mirror ac.AcState — same null handling, same "absent is not zero" rule.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Victron charger states. Shared enum with other charger types.
CHARGER_STATE = {
    0: "off",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize",
    11: "other (hub-1)",
    245: "wake-up",
    247: "auto equalize",
    252: "external control",
}

# States in which the controller is deliberately holding back because the bank
# is charged, rather than because the array cannot deliver.
THROTTLING_STATES = frozenset({5, 6, 252})

# Common SmartSolar error codes. 67 is the one already seen on this rig
# (2026-08-07, since cleared).
CHARGER_ERROR = {
    0: "no error",
    2: "battery voltage too high",
    17: "charger temperature too high",
    18: "charger over-current",
    19: "charger current reversed",
    20: "bulk time limit exceeded",
    21: "current sensor issue",
    26: "terminals overheated",
    33: "input voltage too high",
    34: "input current too high",
    38: "input shutdown (battery voltage high)",
    67: "BMS connection lost",
    116: "calibration data lost",
    119: "settings data lost",
}

_PREFIX = re.compile(r"^N/[^/]+/(?P<rest>.+)$")

_PV_V = re.compile(r"^solarcharger/(?P<inst>\d+)/Pv/V$")
_YIELD_POWER = re.compile(r"^solarcharger/(?P<inst>\d+)/Yield/Power$")
_DC = re.compile(r"^solarcharger/(?P<inst>\d+)/Dc/0/(?P<field>Voltage|Current)$")
_SCALAR = re.compile(r"^solarcharger/(?P<inst>\d+)/(?P<field>State|ErrorCode|Mode|Connected)$")
_DAILY = re.compile(r"^solarcharger/(?P<inst>\d+)/History/Daily/0/(?P<field>Yield|MaxPower)$")
_SYS_PV = re.compile(r"^system/0/Dc/Pv/Power$")


def _num(value) -> float | None:
    """Null means "not applicable", not zero — zero watts at night is a real
    measurement and must stay distinguishable from a missing reading."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass
class ChargerState:
    """Latest values for one solar charge controller."""
    inst: int
    pv_voltage: float | None = None
    pv_power: float | None = None
    dc_voltage: float | None = None
    dc_current: float | None = None
    state: int | None = None
    error_code: int | None = None
    mode: int | None = None
    connected: bool | None = None
    yield_today_kwh: float | None = None
    max_power_today: float | None = None

    @property
    def state_label(self) -> str | None:
        if self.state is None:
            return None
        return CHARGER_STATE.get(self.state, f"unknown ({self.state})")

    @property
    def error_label(self) -> str | None:
        if self.error_code is None:
            return None
        return CHARGER_ERROR.get(self.error_code, f"error {self.error_code}")


@dataclass
class SolarState:
    chargers: dict[int, ChargerState] = field(default_factory=dict)
    system_pv_power: float | None = None
    last_update_ts: int = 0

    def _charger(self, inst: int) -> ChargerState:
        c = self.chargers.get(inst)
        if c is None:
            c = ChargerState(inst=inst)
            self.chargers[inst] = c
        return c

    def update(self, topic: str, value) -> bool:
        """Fold one message in. Returns True if it was a solar topic we track."""
        m = _PREFIX.match(topic)
        if not m:
            return False
        rest = m.group("rest")

        if _SYS_PV.match(rest):
            v = _num(value)
            if v is not None:
                self.system_pv_power = v
                self._stamp()
            return True
        if (mm := _PV_V.match(rest)):
            return self._set(int(mm.group("inst")), "pv_voltage", _num(value))
        if (mm := _YIELD_POWER.match(rest)):
            return self._set(int(mm.group("inst")), "pv_power", _num(value))
        if (mm := _DC.match(rest)):
            attr = "dc_voltage" if mm.group("field") == "Voltage" else "dc_current"
            return self._set(int(mm.group("inst")), attr, _num(value))
        if (mm := _DAILY.match(rest)):
            attr = "yield_today_kwh" if mm.group("field") == "Yield" else "max_power_today"
            return self._set(int(mm.group("inst")), attr, _num(value))
        if (mm := _SCALAR.match(rest)):
            v = _num(value)
            inst, f = int(mm.group("inst")), mm.group("field")
            if v is None:
                self._stamp()
                return True
            c = self._charger(inst)
            if f == "State":
                c.state = int(v)
            elif f == "ErrorCode":
                c.error_code = int(v)
            elif f == "Mode":
                c.mode = int(v)
            else:
                c.connected = bool(v)
            self._stamp()
            return True
        return False

    def _set(self, inst: int, attr: str, v: float | None) -> bool:
        setattr(self._charger(inst), attr, v)
        self._stamp()
        return True

    def _stamp(self) -> None:
        self.last_update_ts = int(time.time())

    # -- derived ------------------------------------------------------------

    @property
    def total_pv_power(self) -> float | None:
        """System-wide PV production. Prefers the Cerbo's own system-level
        figure; falls back to summing controllers."""
        if self.system_pv_power is not None:
            return self.system_pv_power
        vals = [c.pv_power for c in self.chargers.values() if c.pv_power is not None]
        return sum(vals) if vals else None

    @property
    def total_charge_current(self) -> float | None:
        """Current actually delivered to the battery, across controllers."""
        vals = [c.dc_current for c in self.chargers.values() if c.dc_current is not None]
        return sum(vals) if vals else None

    @property
    def total_yield_today_kwh(self) -> float | None:
        vals = [c.yield_today_kwh for c in self.chargers.values()
                if c.yield_today_kwh is not None]
        return sum(vals) if vals else None

    @property
    def max_power_today(self) -> float | None:
        vals = [c.max_power_today for c in self.chargers.values()
                if c.max_power_today is not None]
        return max(vals) if vals else None

    @property
    def primary(self) -> ChargerState | None:
        """Lowest-instance controller — the one whose state/error we surface
        as the headline. Single-controller rigs (this van) have only one."""
        if not self.chargers:
            return None
        return self.chargers[min(self.chargers)]

    @property
    def state_label(self) -> str | None:
        p = self.primary
        return p.state_label if p else None

    @property
    def is_throttling(self) -> bool:
        """True when the controller is holding back because the bank is full.

        The distinction low-yield diagnosis turns on: in Float a small number
        means 'nowhere to put it', in Bulk the same number means 'the array
        genuinely cannot deliver more'.
        """
        p = self.primary
        return p is not None and p.state in THROTTLING_STATES

    @property
    def has_error(self) -> bool:
        return any(c.error_code not in (None, 0) for c in self.chargers.values())

    @property
    def error_label(self) -> str | None:
        for c in self.chargers.values():
            if c.error_code not in (None, 0):
                return c.error_label
        return None

    @property
    def has_data(self) -> bool:
        return self.last_update_ts > 0
