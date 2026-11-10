"""Cross-pack analysis: the brain Cerberus contributes on top of the Cerbo.

Eight detectors, all stateful, emitting ``Event``s. Five consume successive
``BankState`` snapshots and run under ``DetectorRunner``; three take their own
input and are driven separately from ``__main__`` — ``CellImbalanceDetector``
off BLE per-cell data, ``SolarOutputOpenDetector`` off ``SolarState``, and
``ColdChargeDetector`` off the bank plus the solar and shore figures. All take
an explicit ``now`` timestamp so tests can replay history without real clocks.

Edge-triggering + hysteresis is the rule, not the exception: noise on a sensor
or a brief excursion across a threshold must not produce a stream of alerts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, MutableMapping, Protocol

from .events import Event, Severity, make as make_event
from .model import BankState


KV_LAST_FULL_CHARGE = "last_full_charge_ts"
KV_LAST_OVERDUE_REMINDER = "last_overdue_reminder_ts"


class Detector(Protocol):
    def evaluate(self, bank: BankState, now: int) -> list[Event]: ...


# ---------------------------------------------------------------------------
# 1. SoC drift — primary detector
# ---------------------------------------------------------------------------

class DriftDetector:
    """Spread = max(soc) - min(soc). Sustained > threshold → soc_drift warn.

    On a hard-parallel bank, near-equal voltages with divergent SoC are counter
    divergence (the cure is a full charge), not a real capacity imbalance. The
    alert is an informational nudge, not a crisis.
    """

    def __init__(
        self,
        threshold_pct: float = 15.0,
        sustain_seconds: int = 600,
        clear_hysteresis_pct: float = 3.0,
    ):
        self.threshold = float(threshold_pct)
        self.sustain = int(sustain_seconds)
        self.clear_threshold = max(0.0, self.threshold - float(clear_hysteresis_pct))
        self.drift_since: int | None = None
        self.alerted = False

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        spread = bank.soc_spread
        if spread is None:
            return []

        if spread > self.threshold:
            if self.drift_since is None:
                self.drift_since = now
            if not self.alerted and (now - self.drift_since) >= self.sustain:
                self.alerted = True
                return [
                    make_event(
                        "soc_drift",
                        Severity.WARN,
                        {
                            "spread_pct": round(spread, 1),
                            "soc": {str(p): bank.packs[p].soc for p in sorted(bank.packs)},
                        },
                        ts=now,
                    )
                ]
            return []

        if spread <= self.clear_threshold:
            was_alerted = self.alerted
            self.drift_since = None
            self.alerted = False
            if was_alerted:
                return [
                    make_event(
                        "soc_drift_cleared",
                        Severity.INFO,
                        {"spread_pct": round(spread, 1)},
                        ts=now,
                    )
                ]
        # in hysteresis band → leave state alone
        return []


# ---------------------------------------------------------------------------
# 2. Full-charge calibration
# ---------------------------------------------------------------------------

class FullChargeDetector:
    """All packs >= soc_pct, all |current| <= taper_a, and min voltage >=
    absorption_voltage_min ⇒ a full charge is in progress. Rising edge emits
    ``full_charge`` once and stamps ``kv[KV_LAST_FULL_CHARGE]``. Separately,
    days-since-last-full > remind_after_days emits ``full_charge_overdue``
    at most once per day.

    Hysteresis on the rising edge: tapering current oscillates around the
    threshold (observed in production — 4.0A ↔ 6.0A wobble re-fired the
    event 20s later). Once we've latched ``was_full = True``, the condition
    has to remain *un-full* for ``rearm_seconds`` before we'll consider a new
    rising edge. Real charge cycles take much longer than this to re-establish,
    so 5 minutes is comfortable and a brief sensor wobble at the boundary is
    absorbed without re-firing.
    """

    def __init__(
        self,
        kv: MutableMapping[str, str],
        soc_pct: float = 99.0,
        taper_a: float = 5.0,
        absorption_v_min: float = 14.0,
        remind_after_days: float = 30.0,
        rearm_seconds: int = 300,
    ):
        self.kv = kv
        self.soc_pct = float(soc_pct)
        self.taper_a = float(taper_a)
        self.absorption_v_min = float(absorption_v_min)
        self.remind_after_days = float(remind_after_days)
        self.rearm_seconds = int(rearm_seconds)
        self.was_full = False
        self._not_full_since: int | None = None

    def _condition(self, bank: BankState) -> bool:
        if not bank.packs:
            return False
        packs = bank.packs.values()
        if not all(p.soc >= self.soc_pct for p in packs):
            return False
        if not all(abs(p.current) <= self.taper_a for p in packs):
            return False
        if min(p.voltage for p in packs) < self.absorption_v_min:
            return False
        return True

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        events: list[Event] = []
        is_full = self._condition(bank)

        if is_full:
            if not self.was_full:
                # rising edge — emit + latch
                self.kv[KV_LAST_FULL_CHARGE] = str(now)
                self.was_full = True
                events.append(
                    make_event(
                        "full_charge",
                        Severity.INFO,
                        {
                            "soc": {str(p): bank.packs[p].soc for p in sorted(bank.packs)},
                            "voltage_min": min(p.voltage for p in bank.packs.values()),
                        },
                        ts=now,
                    )
                )
            # any time the condition is true, the un-full timer is moot
            self._not_full_since = None
        else:
            # condition is currently false; if we're still latched, accumulate
            # the un-full window before re-arming
            if self.was_full:
                if self._not_full_since is None:
                    self._not_full_since = now
                elif (now - self._not_full_since) >= self.rearm_seconds:
                    self.was_full = False
                    self._not_full_since = None

        # overdue reminder — independent of edge state
        last_full = self._read_int(KV_LAST_FULL_CHARGE)
        if last_full is not None:
            days_since = (now - last_full) / 86400.0
            if days_since > self.remind_after_days:
                last_reminder = self._read_int(KV_LAST_OVERDUE_REMINDER) or 0
                # at most once per day
                if (now - last_reminder) >= 86400:
                    self.kv[KV_LAST_OVERDUE_REMINDER] = str(now)
                    events.append(
                        make_event(
                            "full_charge_overdue",
                            Severity.WARN,
                            {"days_since_full_charge": round(days_since, 1)},
                            ts=now,
                        )
                    )

        return events

    def days_since_full_charge(self, now: int) -> float | None:
        last = self._read_int(KV_LAST_FULL_CHARGE)
        return (now - last) / 86400.0 if last is not None else None

    def _read_int(self, key: str) -> int | None:
        v = self.kv.get(key)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# 3. Current-sharing asymmetry
# ---------------------------------------------------------------------------

class CurrentShareDetector:
    """Replaces the dead cell-imbalance detector. Only evaluates when bank
    |total_current| > min_amps. Each pack's magnitude share of total current
    is expected to be ~1/N. A pack consistently carrying more than
    (1/N) * (1 + excess_pct/100) of the work over the rolling window emits
    ``current_share_imbalance``. Low severity — health signal, not alarm.

    A pack must also exceed its fair share by ``min_excess_amps`` in absolute
    terms. At low bank current the percentage test alone fires on BMS sensor
    resolution: on 2026-10-08 pack 3 read 6.25 A of a ~14 A bank (44% share)
    — only ~1.5 A over fair share, which is noise, not an overworked pack."""

    def __init__(
        self,
        excess_pct: float = 25.0,
        min_amps: float = 10.0,
        sustain_seconds: int = 300,
        min_excess_amps: float = 8.0,
    ):
        self.excess = float(excess_pct) / 100.0
        self.min_amps = float(min_amps)
        self.min_excess_amps = float(min_excess_amps)
        self.sustain = int(sustain_seconds)
        # per-pack state
        self._over_since: dict[int, int | None] = {}
        self._alerted: dict[int, bool] = {}

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        if not bank.packs:
            return []
        total_mag = sum(abs(p.current) for p in bank.packs.values())
        if total_mag < self.min_amps:
            # below activity floor — reset windows so a brief idle period doesn't
            # mask a fresh imbalance later
            for k in list(self._over_since):
                self._over_since[k] = None
            return []

        n = len(bank.packs)
        expected = 1.0 / n
        limit = expected * (1.0 + self.excess)

        events: list[Event] = []
        for pid, p in bank.packs.items():
            share = abs(p.current) / total_mag
            excess_amps = abs(p.current) - total_mag * expected
            self._over_since.setdefault(pid, None)
            self._alerted.setdefault(pid, False)

            if share > limit and excess_amps >= self.min_excess_amps:
                if self._over_since[pid] is None:
                    self._over_since[pid] = now
                if not self._alerted[pid] and (now - self._over_since[pid]) >= self.sustain:
                    self._alerted[pid] = True
                    events.append(
                        make_event(
                            "current_share_imbalance",
                            Severity.INFO,
                            {
                                "pack": pid,
                                "share_pct": round(share * 100.0, 1),
                                "expected_pct": round(expected * 100.0, 1),
                                "current": round(p.current, 2),
                            },
                            ts=now,
                        )
                    )
            else:
                self._over_since[pid] = None
                self._alerted[pid] = False
        return events


# ---------------------------------------------------------------------------
# 4. Temperature (per pack, edge + hysteresis)
# ---------------------------------------------------------------------------

@dataclass
class _TempPackState:
    high_alerted: bool = False
    low_alerted: bool = False


class TemperatureDetector:
    def __init__(
        self,
        high_c: float = 45.0,
        low_c: float = 2.0,
        hysteresis_c: float = 2.0,
    ):
        self.high = float(high_c)
        self.low = float(low_c)
        self.high_clear = self.high - float(hysteresis_c)
        self.low_clear = self.low + float(hysteresis_c)
        self._state: dict[int, _TempPackState] = {}

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        events: list[Event] = []
        for pid, p in bank.packs.items():
            st = self._state.setdefault(pid, _TempPackState())
            t = p.temperature

            if t > self.high and not st.high_alerted:
                st.high_alerted = True
                events.append(
                    make_event("temp_high", Severity.WARN,
                               {"pack": pid, "temperature": t}, ts=now)
                )
            elif t <= self.high_clear and st.high_alerted:
                st.high_alerted = False
                events.append(
                    make_event("temp_high_cleared", Severity.INFO,
                               {"pack": pid, "temperature": t}, ts=now)
                )

            if t < self.low and not st.low_alerted:
                st.low_alerted = True
                events.append(
                    make_event("temp_low", Severity.WARN,
                               {"pack": pid, "temperature": t}, ts=now)
                )
            elif t >= self.low_clear and st.low_alerted:
                st.low_alerted = False
                events.append(
                    make_event("temp_low_cleared", Severity.INFO,
                               {"pack": pid, "temperature": t}, ts=now)
                )
        return events


# ---------------------------------------------------------------------------
# 4b. Charging a cold pack
# ---------------------------------------------------------------------------

class ColdChargeDetector:
    """Current going into a pack that is below ``low_c``.

    ``TemperatureDetector`` says a pack is cold; this says it is being charged
    while cold, which is what damages LiFePO4 (lithium plating). Added
    2026-11-10 for the start of a winter trip: the packs are switched on cold
    because the furnace runs off them, and the alternator charges them through
    the ACR before the cabinet warms. The ACR has no temperature cut-off, and
    the Victron chargers only get one through DVCC's shared temperature sense.

    The source is named so the owner knows what to stop: shore when the
    MultiPlus is charging from AC, otherwise whichever of solar and the rest
    (bank current minus solar current, i.e. the alternator) is larger. With no
    solar reading yet the source is "unknown" rather than a guess.

    Both edges wait ``sustain_seconds``, so a single odd reading neither raises
    nor clears it. Clearing needs the packs ``hysteresis_c`` warmer than
    ``low_c`` or the charge current back under ``min_charge_a``. Packs reading
    under ``implausible_voltage`` are a missing reading and are skipped: a
    missing temperature parses as 0.0 C.
    """

    def __init__(self, low_c: float = 5.0, min_charge_a: float = 5.0,
                 sustain_seconds: int = 60, hysteresis_c: float = 2.0,
                 implausible_voltage: float = 5.0):
        self.low = float(low_c)
        self.clear_c = self.low + float(hysteresis_c)
        self.min_a = float(min_charge_a)
        self.sustain = int(sustain_seconds)
        self.implausible_v = float(implausible_voltage)
        self.alerted = False
        self._since: int | None = None      # when the pending edge began

    def evaluate(self, bank: BankState, now: int, solar_a: float | None = None,
                 shore: bool = False) -> list[Event]:
        packs = [p for p in bank.packs.values() if p.voltage >= self.implausible_v]
        if not packs:
            return []
        coldest = min(packs, key=lambda p: p.temperature)
        amps = bank.total_current
        if self.alerted:
            edge = coldest.temperature >= self.clear_c or amps < self.min_a
        else:
            edge = coldest.temperature < self.low and amps >= self.min_a
        if not edge:
            self._since = None
            return []
        if self._since is None:
            self._since = now
        if now - self._since < self.sustain:
            return []
        self._since = None
        self.alerted = not self.alerted
        detail = {"pack": coldest.pack_id, "temperature": coldest.temperature,
                  "current": round(amps, 1)}
        if not self.alerted:
            return [make_event("cold_charge_cleared", Severity.INFO, detail, ts=now)]
        detail["source"] = self._source(amps, solar_a, shore)
        return [make_event("cold_charge", Severity.WARN, detail, ts=now)]

    @staticmethod
    def _source(amps: float, solar_a: float | None, shore: bool) -> str:
        if shore:
            return "shore"
        if solar_a is None:
            return "unknown"
        return "solar" if solar_a >= amps - solar_a else "alternator"


# ---------------------------------------------------------------------------
# 5. Low charge
# ---------------------------------------------------------------------------

class LowChargeDetector:
    """min(soc) < low_soc_pct ⇒ low_soc (warn). If any pack voltage drops near
    the 12.1V Lithionics cutoff (default 12.3V) escalate to low_soc_crit. Both
    are edge-triggered with hysteresis on SoC + voltage.

    Pack voltages below ``implausible_voltage`` are ignored: they are a missing
    reading, not a flat pack. A pack element that arrives with soc but no
    voltage parses as 0.0 V, and on 2026-10-29 17:24:56 one did, as pack 1
    dropped off the bus at 99% SoC: a CRIT that cleared 5 s later at 13.3 V.
    A real pack that low has long since been disconnected by its own BMS."""

    def __init__(
        self,
        low_soc_pct: float = 15.0,
        crit_voltage: float = 12.3,
        soc_hysteresis_pct: float = 3.0,
        voltage_hysteresis: float = 0.1,
        implausible_voltage: float = 5.0,
    ):
        self.low = float(low_soc_pct)
        self.low_clear = self.low + float(soc_hysteresis_pct)
        self.crit_v = float(crit_voltage)
        self.crit_v_clear = self.crit_v + float(voltage_hysteresis)
        self.implausible_v = float(implausible_voltage)
        self.warn_alerted = False
        self.crit_alerted = False

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        if not bank.packs:
            return []
        min_soc = min(p.soc for p in bank.packs.values())
        volts = [p.voltage for p in bank.packs.values() if p.voltage >= self.implausible_v]
        min_v = min(volts) if volts else None
        events: list[Event] = []

        if min_soc < self.low and not self.warn_alerted:
            self.warn_alerted = True
            events.append(
                make_event("low_soc", Severity.WARN,
                           {"min_soc": min_soc,
                            "min_voltage": None if min_v is None else round(min_v, 3)},
                           ts=now)
            )
        elif min_soc >= self.low_clear and self.warn_alerted:
            self.warn_alerted = False
            events.append(
                make_event("low_soc_cleared", Severity.INFO,
                           {"min_soc": min_soc}, ts=now)
            )

        if min_v is None:
            return events     # no real voltage this round: leave crit state as is
        if min_v <= self.crit_v and not self.crit_alerted:
            self.crit_alerted = True
            events.append(
                make_event("low_soc_crit", Severity.CRIT,
                           {"min_voltage": round(min_v, 3), "min_soc": min_soc}, ts=now)
            )
        elif min_v >= self.crit_v_clear and self.crit_alerted:
            self.crit_alerted = False
            events.append(
                make_event("low_soc_crit_cleared", Severity.INFO,
                           {"min_voltage": round(min_v, 3)}, ts=now)
            )
        return events


# ---------------------------------------------------------------------------
# 6. Cell imbalance (BLE-sourced — fed separately from BankState detectors)
# ---------------------------------------------------------------------------

class CellImbalanceDetector:
    """Per-pack and cross-pack cell-voltage spread. Fed by BLE-side per-cell
    data — not driven by ``BankState``. Edge-triggered with hysteresis like
    the other detectors. Per-pack imbalance ⇒ ``cell_imbalance``; whole-bank
    spread ⇒ ``cell_imbalance_bank``."""

    def __init__(
        self,
        in_pack_threshold_mv: float = 50.0,
        cross_pack_threshold_mv: float = 100.0,
        sustain_seconds: int = 60,
        clear_hysteresis_mv: float = 10.0,
    ):
        self.in_pack = float(in_pack_threshold_mv)
        self.cross_pack = float(cross_pack_threshold_mv)
        self.sustain = int(sustain_seconds)
        self.in_pack_clear = max(0.0, self.in_pack - float(clear_hysteresis_mv))
        self.cross_pack_clear = max(0.0, self.cross_pack - float(clear_hysteresis_mv))
        self._pack_over_since: dict[int, int | None] = {}
        self._pack_alerted: dict[int, bool] = {}
        self._bank_over_since: int | None = None
        self._bank_alerted: bool = False

    def evaluate(
        self,
        cells_mv: Mapping[int, list[int]],
        now: int,
    ) -> list[Event]:
        if not cells_mv:
            return []
        events: list[Event] = []

        # Per-pack
        for pid, cells in cells_mv.items():
            if not cells:
                continue
            spread = max(cells) - min(cells)
            self._pack_over_since.setdefault(pid, None)
            self._pack_alerted.setdefault(pid, False)

            if spread > self.in_pack:
                if self._pack_over_since[pid] is None:
                    self._pack_over_since[pid] = now
                if (not self._pack_alerted[pid]
                        and (now - self._pack_over_since[pid]) >= self.sustain):
                    self._pack_alerted[pid] = True
                    events.append(make_event(
                        "cell_imbalance", Severity.WARN,
                        {
                            "pack": pid,
                            "spread_mv": int(spread),
                            "cell_max_mv": int(max(cells)),
                            "cell_min_mv": int(min(cells)),
                        },
                        ts=now,
                    ))
            elif spread <= self.in_pack_clear:
                was_alerted = self._pack_alerted[pid]
                self._pack_over_since[pid] = None
                self._pack_alerted[pid] = False
                if was_alerted:
                    events.append(make_event(
                        "cell_imbalance_cleared", Severity.INFO,
                        {"pack": pid, "spread_mv": int(spread)},
                        ts=now,
                    ))

        # Cross-pack: max of all cells minus min of all cells
        all_cells = [v for cells in cells_mv.values() for v in cells]
        if all_cells:
            bank_spread = max(all_cells) - min(all_cells)
            if bank_spread > self.cross_pack:
                if self._bank_over_since is None:
                    self._bank_over_since = now
                if (not self._bank_alerted
                        and (now - self._bank_over_since) >= self.sustain):
                    self._bank_alerted = True
                    events.append(make_event(
                        "cell_imbalance_bank", Severity.WARN,
                        {
                            "spread_mv": int(bank_spread),
                            "cell_max_mv": int(max(all_cells)),
                            "cell_min_mv": int(min(all_cells)),
                        },
                        ts=now,
                    ))
            elif bank_spread <= self.cross_pack_clear:
                was_alerted = self._bank_alerted
                self._bank_over_since = None
                self._bank_alerted = False
                if was_alerted:
                    events.append(make_event(
                        "cell_imbalance_bank_cleared", Severity.INFO,
                        {"spread_mv": int(bank_spread)},
                        ts=now,
                    ))
        return events


# ---------------------------------------------------------------------------
# 7. Solar output open (tripped breaker / blown fuse / lost connection)
# ---------------------------------------------------------------------------

# Solar-open thresholds relative to the array (config.yaml `solar`). This
# rig's 300 W of 12 V-class panels in parallel, with an open-circuit voltage of
# ~20 V measured in full sun (daily peak 19.7-20.5 V; panel labels read 5-10%
# higher), gives exactly the values the detector was tuned with below.
ARRAY_VOC_V = 20.0
ARRAY_W = 300.0
PV_MIN_FRAC = 0.75           # 15.0 V: sun vs night
OPEN_CIRCUIT_FRAC = 0.925    # 18.5 V: above every twilight alert (max 17.2 V)
RECENT_POWER_DIV = 12        # 25 W: "the array was just working"


def solar_open_thresholds(array_voc_v: float = ARRAY_VOC_V,
                          array_w: float = ARRAY_W) -> dict[str, float]:
    """config.yaml threshold keys derived from the array, for a config that
    leaves them unset."""
    return {
        "solar_open_pv_voltage_min": round(array_voc_v * PV_MIN_FRAC, 2),
        "solar_open_circuit_voltage": round(array_voc_v * OPEN_CIRCUIT_FRAC, 2),
        "solar_open_recent_power_w": round(array_w / RECENT_POWER_DIV, 2),
    }


class SolarOutputOpenDetector:
    """The array is lit but nothing is drawing from it — the *battery-side*
    path out of the MPPT is open. Fed by ``SolarState``, not ``BankState``.

    Written for the degraded 30 A solar output breaker on this rig, which
    trips at 13.2-14.3 A on any clear morning and does not auto-reset. Every
    recovery in the data is a manual reset, and the cost of not noticing has
    been hours: 3h12m on 2026-08-23, 2h50m on 2026-08-24, but only 5 minutes
    on 2026-08-29 when someone happened to be in the van. That gap is the
    whole reason this detector exists.

    The signature is unambiguous and the MPPT does *not* report it::

        pv_voltage   19.8-20.8 V   array at open-circuit, still in sun
        pv_power          0.0 W    nothing being drawn
        charge_current    0.0 A    nothing reaching the battery
        error_code            0    the controller sees no fault at all

    ``error_code == 0`` is required, not incidental. The fault is *external* to
    the MPPT, so a non-zero code means something else is wrong and that
    condition deserves its own alert rather than being relabelled as this one.

    Two things this must not fire on:

    * **Night.** PV reads ~0.4 V with the array dark, so ``pv_voltage_min``
      separates "no sun" from "sun, no path". This is also what distinguishes
      an *output*-side open from an *input*-side one: if the input breaker
      tripped, PV voltage would read 0.
    * **A genuinely full bank.** A controller that has finished charging can
      also sit at 0 W with PV floating up to Voc. ``full_soc_guard_pct``
      suppresses the alert when every pack is near full, which is the physical
      discriminator — an open output looks identical from the solar tree alone,
      and only the bank tells you whether stopping was on purpose. Absent bank
      data does *not* suppress: an unknown SoC is not a full one.
    * **Another charge source holding the bus up.** This is the one that bit us.
      While the alternator charges at 117-179 A it pins the DC bus near 13.5 V,
      the MPPT reads that as a satisfied battery, drops to float and produces
      0 W, and PV floats to 19.8-21.0 V — byte-for-byte the trip signature.
      Two false alarms on 2026-08-29 (12:41, 14:25) came from exactly this while
      driving. ``charging_guard_a`` suppresses when the bank is taking current
      from somewhere other than this controller.

      The threshold sits well above what the array can deliver (~15 A peak on
      this rig) so solar's own contribution never trips the guard. (Strictly
      it need not: the signature already requires this controller to be
      producing nothing, so any charge current is another source's. Config
      therefore defaults it to drives' charge_on_a, the same "another charger
      is on" threshold the drive and solar-path checks use: 20 A here.) Suppressing
      here is right on the merits, not just convenient: with another source
      charging, an open solar output is costing nothing at that moment, and it
      will be caught as soon as that source stops. Verified against the real
      trips the same morning, which ran at -8 to -13.5 A and so are unaffected.
    * **Twilight.** The one that made this detector unusable. ``pv_voltage_min``
      was set at 15 V on the reasoning that night reads ~0.4 V and a trip reads
      19.8-20.8 V, so 15 V sat safely between them. That is true at midday and
      false at dawn and dusk, because the array's open-circuit voltage *ramps
      through* the 15-18 V band twice a day while it is still too dim to make
      power. Of the 61 alerts raised up to 2026-09-18, **50 fired between 06:30
      and 06:50 or between 18:20 and 19:34** — tracking sunrise and sunset, not
      the breaker. They drifted 19 minutes later over the 13 days from 09-05 to
      09-18, which is the season, not a fault.

      The fix is not a higher fixed threshold. It is to require positive
      evidence that the array *could* be producing right now — see
      ``_capable`` below.

    Deciding whether the array is capable
    -------------------------------------
    Voltage alone cannot answer this: at full sun with a satisfied battery the
    MPPT idles and PV floats to the same ~20 V a trip produces, and in heavy
    overcast a genuine trip may never reach 20 V at all. Two independent
    signals each settle it on their own, so either suffices:

    * **PV at open-circuit.** ``open_circuit_voltage`` (default 18.5 V) is above
      every twilight alert ever recorded (max 17.2 V) and below both confirmed
      trips (20.4, 20.5 V) and the array's daily peak Voc (19.7-20.5 V every day
      in the record). Reaching it means the sun is properly up.
    * **The array was just working.** ``recent_power_w`` within
      ``recent_window_seconds``: power that stopped is a step change, which is
      what a breaker does. In the 10 minutes before the two real trips the array
      made **217.9 W and 234.6 W**; before all 59 other alerts it made **at most
      8.0 W**. This is what catches a trip on a dim day, where Voc never climbs
      to 18.5 V.

    The capability test gates the **rising edge only**. Once alerted, the outage
    continues until power actually returns, so a trip that persists past sunset
    is not "cleared" by the light going away — and a trip left unreset overnight
    raises a fresh alert the next morning as soon as the sun lifts PV to Voc.
    That is the multi-hour outage this detector was built for, and gating the
    continuation as well would have silently reopened it.

    On a cold start the power history is empty, so only the voltage signal is
    available for the first ``recent_window_seconds``. That is the safe
    direction: it can delay an alert on a dim day, never invent one.
    """

    def __init__(
        self,
        pv_voltage_min: float = 15.0,
        pv_power_max: float = 1.0,
        charge_current_max: float = 0.5,
        sustain_seconds: int = 90,
        full_soc_guard_pct: float = 97.0,
        charging_guard_a: float = 20.0,
        open_circuit_voltage: float = 18.5,
        recent_power_w: float = 25.0,
        recent_window_seconds: int = 600,
        voc_vmp_ratio: float = 1.15,
        float_state: int = 5,
    ):
        self.pv_voltage_min = float(pv_voltage_min)
        self.pv_power_max = float(pv_power_max)
        self.charge_current_max = float(charge_current_max)
        self.sustain = int(sustain_seconds)
        self.full_soc_guard = float(full_soc_guard_pct)
        self.charging_guard_a = float(charging_guard_a)
        self.open_circuit_voltage = float(open_circuit_voltage)
        self.recent_power_w = float(recent_power_w)
        self.recent_window = int(recent_window_seconds)
        self.voc_vmp_ratio = float(voc_vmp_ratio)
        self.float_state = int(float_state)
        self.open_since: int | None = None
        self.alerted = False
        # (ts, pv_power, pv_voltage) over the trailing recent_window. Bounded
        # by time, not by count, so the sample rate can change without changing
        # behaviour.
        self._power_history: deque[tuple[int, float, float]] = deque()

    def _record(self, solar, now: int) -> None:
        """Fold this sample into the trailing history, then prune it."""
        pv_w = solar.total_pv_power
        pv_v = solar.primary.pv_voltage if solar.primary is not None else None
        if pv_w is not None and pv_v is not None:
            self._power_history.append((now, float(pv_w), float(pv_v)))
        cutoff = now - self.recent_window
        while self._power_history and self._power_history[0][0] < cutoff:
            self._power_history.popleft()

    def _last_loaded_voltage(self) -> float | None:
        """PV voltage the last time the array was demonstrably under load.

        This is the array's maximum-power-point voltage in the conditions of a
        few minutes ago — a live reference that already accounts for the sun
        angle, the cloud cover and the panel temperature, none of which any
        fixed threshold can track.
        """
        for _, w, v in reversed(self._power_history):
            if w >= self.recent_power_w:
                return v
        return None

    def _finished_on_purpose(self, primary) -> bool:
        """Is the controller idle because it *chose* to be, not because the
        path is broken?

        The discriminator is the MPPT's own reported state, and it is the only
        one left once the rewire lowered the loaded array voltage. Everything
        else about a legitimate float at 0 W is byte-identical to a trip: PV
        relaxed to Voc, no power, no current, error_code 0.

        **Bank voltage does not separate them**, which is worth recording
        because it is the obvious idea and it is wrong. Measured 2026-09-20
        over the whole dataset::

            float, <=1 W, SoC<97   13.152 V mean, range 13.033-13.267
            night, no charger      13.194 V mean

        The two overlap almost exactly, and the physics says they must: at 0 W
        the controller passes no current, so a healthy float is not *holding*
        the bank up either. It is simply sitting at rest next to it.

        State does separate them. Reaching float requires having completed an
        absorption cycle, which requires having actually charged the bank —
        something an open output cannot have done. With the output open the
        controller reads its own disconnected terminals as a satisfied battery
        and reports **absorption**; both confirmed outages show it::

            2026-08-23 10:42   PV 19.9 V   0.0 W   state 4
            2026-08-24 10:15   PV 19.6 V   0.0 W   state 4

        Across every sample matching the open-output signature, state 5
        accounts for 71 of them and states 3/4 for 95 — the 71 are the
        false-positive regime this removes.

        Gates the **rising edge only**, like the capability test. An outage
        that begins in absorption and later drifts into float keeps its alert;
        only a *new* alert is suppressed. The bounded cost is a trip that
        starts while already in float, which goes unreported until the next
        morning's bulk cycle re-raises it — and at float the array was
        delivering nothing anyway, so that window costs no harvest.
        """
        state = getattr(primary, "state", None)
        return state is not None and int(state) == self.float_state

    def _looks_open(self, pv_v: float) -> bool:
        """Has the array come *off load*, as opposed to run out of light?

        This is the distinction the voltage threshold could never draw, and it
        is what separates a tripped breaker from a passing cloud — the two look
        identical in power, which is zero either way, and opposite in voltage:

        * **Open output.** Nothing is drawing, so the array relaxes from its
          maximum-power point up to open circuit. Voltage *rises*, by the
          Voc/Vmp ratio of the cells (~1.2 for silicon). On 2026-08-29 the trip
          took PV from 16.6 V under 201 W straight to 20.7 V at 0 W.
        * **Cloud, or dusk.** The light is simply gone, so the array cannot hold
          voltage either. Power and voltage fall *together*. Both 08-27 alerts —
          07:49 and 13:44, each a cloud that cleared within the half hour — sat
          at or below the voltage the array had been working at minutes before.

        With no recent load to compare against (overnight, or a cold start) there
        is no reference, so fall back to requiring a true open-circuit reading:
        ``open_circuit_voltage`` is above every twilight alert on record (max
        17.2 V) and below the array's daily peak Voc (19.7-20.5 V every day).
        That fallback is what re-raises a trip left unreset overnight, once the
        morning sun lifts PV to open circuit.
        """
        loaded_v = self._last_loaded_voltage()
        if loaded_v is not None:
            return pv_v >= loaded_v * self.voc_vmp_ratio
        return pv_v >= self.open_circuit_voltage

    def _signature(self, solar, bank: BankState | None) -> bool:
        """The electrical signature of an open output, capability aside."""
        primary = solar.primary
        if primary is None:
            return False

        # "Absent is not zero" — a missing reading must never be read as a
        # zero one, or a partially-populated tree at startup looks like a trip.
        pv_v = primary.pv_voltage
        pv_w = solar.total_pv_power
        amps = solar.total_charge_current
        if pv_v is None or pv_w is None or amps is None:
            return False
        if primary.error_code is None or primary.error_code != 0:
            return False

        if pv_v < self.pv_voltage_min:
            return False
        if pv_w > self.pv_power_max or abs(amps) > self.charge_current_max:
            return False

        if bank is not None and bank.packs:
            # Bank full ⇒ the controller stopping is expected, not a fault.
            if min(p.soc for p in bank.packs.values()) >= self.full_soc_guard:
                return False
            # Another source is charging ⇒ it is holding the bus above the
            # MPPT's float point, which is why this controller is idle.
            if bank.total_current >= self.charging_guard_a:
                return False
        return True

    def evaluate(self, solar, bank: BankState | None, now: int) -> list[Event]:
        if not getattr(solar, "has_data", False):
            return []
        self._record(solar, now)

        # Capability gates the rising edge only. Once alerted, the outage runs
        # until power actually comes back — otherwise a trip that outlasts the
        # daylight would report itself "cleared" by sunset, and the history
        # filling with zeros during a long outage would do the same at noon.
        open_now = self._signature(solar, bank)
        if open_now and not self.alerted:
            open_now = (
                self._looks_open(solar.primary.pv_voltage)
                and not self._finished_on_purpose(solar.primary)
            )

        if open_now:
            if self.open_since is None:
                self.open_since = now
            if not self.alerted and (now - self.open_since) >= self.sustain:
                self.alerted = True
                primary = solar.primary
                return [
                    make_event(
                        "solar_output_open",
                        Severity.WARN,
                        {
                            "pv_voltage": round(primary.pv_voltage, 1),
                            "pv_power": round(solar.total_pv_power, 1),
                            "charge_current": round(solar.total_charge_current, 2),
                            "state": primary.state,
                            "state_label": primary.state_label,
                            "open_seconds": now - self.open_since,
                        },
                        ts=now,
                    )
                ]
            return []

        was_alerted = self.alerted
        open_since = self.open_since
        self.open_since = None
        self.alerted = False
        if was_alerted:
            return [
                make_event(
                    "solar_output_open_cleared",
                    Severity.INFO,
                    {
                        "pv_power": round(solar.total_pv_power or 0.0, 1),
                        "charge_current": round(solar.total_charge_current or 0.0, 2),
                        "outage_seconds": (now - open_since) if open_since else None,
                    },
                    ts=now,
                )
            ]
        return []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class DetectorRunner:
    drift: DriftDetector
    full_charge: FullChargeDetector
    current_share: CurrentShareDetector
    temperature: TemperatureDetector
    low_charge: LowChargeDetector

    def evaluate(self, bank: BankState, now: int) -> list[Event]:
        events: list[Event] = []
        events.extend(self.drift.evaluate(bank, now))
        events.extend(self.full_charge.evaluate(bank, now))
        events.extend(self.current_share.evaluate(bank, now))
        events.extend(self.temperature.evaluate(bank, now))
        events.extend(self.low_charge.evaluate(bank, now))
        return events


def from_config(
    thresholds: Mapping[str, float],
    full_charge_cfg: Mapping[str, float],
    kv: MutableMapping[str, str],
) -> DetectorRunner:
    return DetectorRunner(
        drift=DriftDetector(
            threshold_pct=float(thresholds.get("soc_drift_pct", 15)),
            sustain_seconds=int(float(thresholds.get("soc_drift_sustain_min", 10)) * 60),
        ),
        full_charge=FullChargeDetector(
            kv=kv,
            soc_pct=float(full_charge_cfg.get("soc_pct", 99)),
            taper_a=float(full_charge_cfg.get("taper_current_a", 5)),
            absorption_v_min=float(full_charge_cfg.get("absorption_voltage_min", 14.0)),
            remind_after_days=float(full_charge_cfg.get("remind_after_days", 30)),
            rearm_seconds=int(full_charge_cfg.get("rearm_seconds", 300)),
        ),
        current_share=CurrentShareDetector(
            excess_pct=float(thresholds.get("current_share_excess_pct", 25)),
            min_amps=float(thresholds.get("current_share_min_amps", 10)),
            min_excess_amps=float(thresholds.get("current_share_min_excess_amps", 8)),
        ),
        temperature=TemperatureDetector(
            high_c=float(thresholds.get("temp_high_c", 45)),
            low_c=float(thresholds.get("temp_low_c", 2)),
        ),
        low_charge=LowChargeDetector(
            low_soc_pct=float(thresholds.get("low_soc_pct", 15)),
            crit_voltage=float(thresholds.get("low_voltage_crit_v", 12.3)),
        ),
    )
