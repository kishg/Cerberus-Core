from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Mapping

UC_RE = re.compile(r"_(uc\d+)_")
BATTERY_ID_PREFIX = "com.victronenergy.battery."


@dataclass(frozen=True)
class PackState:
    pack_id: int
    uc: str
    instance: int
    soc: float
    voltage: float
    current: float
    power: float
    temperature: float
    timetogo: int | None
    ts: int
    # State of health and rated capacity. Optional and defaulted because they
    # do NOT arrive in the system/0/Batteries array — that payload carries only
    # soc/voltage/current/power/temperature/timetogo/state. Soh and Capacity
    # live on the per-instance topics N/<portal>/battery/<inst>/{Soh,Capacity},
    # so parse_batteries_array cannot populate them and the collector merges
    # them in by instance afterwards. None means "not heard yet", never zero.
    soh: float | None = None
    capacity_ah: float | None = None


SOURCE_PACKS = "packs"
SOURCE_MONITOR = "monitor"
BANK_SOURCES = (SOURCE_PACKS, SOURCE_MONITOR)


@dataclass(frozen=True)
class BankState:
    ts: int
    packs: dict[int, PackState]
    # Where the figures come from. SOURCE_PACKS: one BMS service per pack, and
    # the bank is their sum (this rig). SOURCE_MONITOR: a single battery
    # monitor (SmartShunt/BMV, or one BMS for the whole bank) that already
    # measures the bank, carried as the lone entry pack_id 1.
    source: str = SOURCE_PACKS

    @property
    def soc_spread(self) -> float | None:
        socs = [p.soc for p in self.packs.values()]
        if not socs:
            return None
        return max(socs) - min(socs)

    @property
    def total_current(self) -> float:
        return sum(p.current for p in self.packs.values())

    @property
    def total_power(self) -> float:
        """Bank power, summed across every pack.

        The Cerbo's own ``system/0/Dc/Battery/Power`` is NOT this number — it
        reports only the pack holding ``active_battery_service`` (pack 1 here),
        so it understates a 3-pack bank by roughly 3x. Verified 2026-06-16
        07:41:10: the Cerbo published 698 W while the packs actually summed to
        2214 W. Anything wanting true bank power must use this, not the Cerbo's
        system topic.
        """
        return sum(p.power for p in self.packs.values())


def _uc_from_id(element_id: str) -> str | None:
    m = UC_RE.search(element_id)
    return m.group(1) if m else None


def parse_batteries_array(
    value: list[dict],
    packs_map: Mapping[str, int],
    ts: int | None = None,
) -> BankState:
    """Parse value of N/<portal>/system/0/Batteries into a BankState.

    The array mixes battery elements with the vebus/MultiPlus element. Filter
    by id prefix (com.victronenergy.battery.) and require an soc field; the
    inverter element carries neither. Map physical packs off the uc token in
    the id (most stable; tied to the BMS unit).
    """
    ts = ts if ts is not None else int(time.time())
    packs: dict[int, PackState] = {}
    for el in value:
        eid = el.get("id", "")
        if not eid.startswith(BATTERY_ID_PREFIX):
            continue
        if "soc" not in el:
            continue
        uc = _uc_from_id(eid)
        if uc is None:
            continue
        pack_id = packs_map.get(uc)
        if pack_id is None:
            continue
        ttg = el.get("timetogo")
        packs[pack_id] = PackState(
            pack_id=pack_id,
            uc=uc,
            instance=int(el.get("instance", -1)),
            soc=float(el["soc"]),
            voltage=float(el.get("voltage", 0.0)),
            current=float(el.get("current", 0.0)),
            power=float(el.get("power", 0.0)),
            temperature=float(el.get("temperature", 0.0)),
            timetogo=int(ttg) if ttg is not None else None,
            ts=ts,
        )
    return BankState(ts=ts, packs=packs)


def parse_battery_monitor(
    value: list[dict],
    instance: int | None = None,
    ts: int | None = None,
) -> BankState:
    """Parse system/0/Batteries for a bank measured by one battery monitor.

    The array lists every battery service the GX sees, so the monitor must be
    picked out: by ``instance`` when configured, else the element the GX
    itself uses as the system battery (``active_battery_service``), else the
    only battery element if there is just one. Anything else is ambiguous and
    yields an empty bank, which the caller logs and skips — guessing would put
    one pack's figures on the bank, the very understatement
    ``BankState.total_power`` exists to avoid.
    """
    ts = ts if ts is not None else int(time.time())
    batteries = [
        el for el in value
        if str(el.get("id", "")).startswith(BATTERY_ID_PREFIX) and "soc" in el
    ]
    if instance is not None:
        chosen = [el for el in batteries if el.get("instance") == instance]
    else:
        chosen = [el for el in batteries if el.get("active_battery_service")]
        if not chosen and len(batteries) == 1:
            chosen = batteries
    if len(chosen) != 1:
        return BankState(ts=ts, packs={}, source=SOURCE_MONITOR)
    el = chosen[0]
    ttg = el.get("timetogo")
    monitor = PackState(
        pack_id=1,
        uc="",
        instance=int(el.get("instance", -1)),
        soc=float(el["soc"]),
        voltage=float(el.get("voltage", 0.0)),
        current=float(el.get("current", 0.0)),
        power=float(el.get("power", 0.0)),
        temperature=float(el.get("temperature", 0.0)),
        timetogo=int(ttg) if ttg is not None else None,
        ts=ts,
    )
    return BankState(ts=ts, packs={1: monitor}, source=SOURCE_MONITOR)
