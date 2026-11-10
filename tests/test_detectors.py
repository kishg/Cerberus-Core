"""Detector tests. Acceptance for M2 per spec §8."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cerberus import detectors as det
from cerberus.events import Severity
from cerberus.model import BankState, PackState, parse_batteries_array

PACKS = {"uc1000001": 1, "uc1000002": 2, "uc1000003": 3}
FIXTURES = Path(__file__).parent / "fixtures"


def _load_messages(name: str) -> list[list[dict]]:
    out: list[list[dict]] = []
    with (FIXTURES / name).open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line)["value"])
    return out


def _pack(pid: int, soc: float, voltage: float, current: float, temp: float = 28.0,
          ts: int = 0) -> PackState:
    return PackState(
        pack_id=pid, uc=f"uc11455{pid:02d}", instance=pid, soc=soc,
        voltage=voltage, current=current, power=current * voltage,
        temperature=temp, timetogo=0, ts=ts,
    )


def _bank(*packs: PackState, ts: int = 0) -> BankState:
    return BankState(ts=ts, packs={p.pack_id: p for p in packs})


# ---------------------------------------------------------------------------
# DriftDetector
# ---------------------------------------------------------------------------

def test_drift_fires_on_real_drift_fixture_after_sustain():
    """drift_discharging fixture: spread 39 — well above 15. Drift must fire
    after the sustain window elapses."""
    values = _load_messages("drift_discharging.jsonl")
    bank = parse_batteries_array(values[0], PACKS)
    assert bank.soc_spread == pytest.approx(39.0)

    d = det.DriftDetector(threshold_pct=15, sustain_seconds=600)
    # first eval at t=0: starts the drift_since clock, does NOT fire
    assert d.evaluate(bank, now=0) == []
    # before sustain elapses: still silent
    assert d.evaluate(bank, now=599) == []
    # at/after sustain: fires once
    events = d.evaluate(bank, now=600)
    assert len(events) == 1
    assert events[0].type == "soc_drift"
    assert events[0].severity == Severity.WARN
    # second eval after firing: no duplicate
    assert d.evaluate(bank, now=900) == []


def test_drift_fires_on_near_full_postdrive_fixture():
    """near_full_postdrive: spread 37 — pack 3 lags hard despite 1/2 being
    near full. Drift must fire."""
    values = _load_messages("near_full_postdrive.jsonl")
    bank = parse_batteries_array(values[0], PACKS)
    assert bank.soc_spread == pytest.approx(37.0)

    d = det.DriftDetector(threshold_pct=15, sustain_seconds=600)
    assert d.evaluate(bank, now=0) == []
    events = d.evaluate(bank, now=600)
    assert len(events) == 1 and events[0].type == "soc_drift"


def test_drift_clears_with_hysteresis_and_emits_cleared():
    d = det.DriftDetector(threshold_pct=15, sustain_seconds=10, clear_hysteresis_pct=3)
    high = _bank(_pack(1, 90, 13.2, 0), _pack(2, 50, 13.2, 0))   # spread 40
    mid = _bank(_pack(1, 90, 13.2, 0), _pack(2, 76, 13.2, 0))    # spread 14 (in band — no change)
    low = _bank(_pack(1, 90, 13.2, 0), _pack(2, 78, 13.2, 0))    # spread 12 (≤ clear thresh)

    d.evaluate(high, now=0)
    fired = d.evaluate(high, now=10)
    assert len(fired) == 1 and fired[0].type == "soc_drift"

    # hysteresis band: still considered drifting, no cleared event
    assert d.evaluate(mid, now=20) == []
    assert d.alerted is True

    cleared = d.evaluate(low, now=30)
    assert len(cleared) == 1 and cleared[0].type == "soc_drift_cleared"
    assert d.alerted is False


def test_drift_does_not_fire_below_threshold():
    d = det.DriftDetector(threshold_pct=15, sustain_seconds=10)
    bank = _bank(_pack(1, 80, 13.2, 0), _pack(2, 70, 13.2, 0))  # spread 10
    assert d.evaluate(bank, now=0) == []
    assert d.evaluate(bank, now=100) == []


# ---------------------------------------------------------------------------
# FullChargeDetector
# ---------------------------------------------------------------------------

def test_full_charge_does_not_fire_on_drift_discharging_fixture():
    """drift_discharging is at ~13.1V, packs 62/64/25, discharging. Nothing
    about it qualifies as a full charge."""
    values = _load_messages("drift_discharging.jsonl")
    fc = det.FullChargeDetector(kv={})
    for i, value in enumerate(values):
        bank = parse_batteries_array(value, PACKS)
        assert fc.evaluate(bank, now=i) == [], f"unexpected event at line {i}"


def test_full_charge_does_not_fire_on_near_full_postdrive():
    """The trap case: packs 1/2 at 96/98 look close to full, but pack 3 is at
    61% and the bank is discharging at float voltage. Must NOT misfire."""
    values = _load_messages("near_full_postdrive.jsonl")
    fc = det.FullChargeDetector(kv={})
    for i, value in enumerate(values):
        bank = parse_batteries_array(value, PACKS)
        assert fc.evaluate(bank, now=i) == [], f"unexpected event at line {i}"


def test_full_charge_fires_once_on_synthetic_rising_edge():
    """Synthetic fixture: line 0 not-yet-full (current 20A, soc 98),
    lines 1+ all packs >=99%, tapered <5A, voltage 14.4. Must fire
    exactly once on the rising edge."""
    values = _load_messages("full_charge_synth.jsonl")
    kv: dict[str, str] = {}
    fc = det.FullChargeDetector(kv=kv)

    fired = 0
    for i, value in enumerate(values):
        bank = parse_batteries_array(value, PACKS)
        evs = fc.evaluate(bank, now=1_000_000 + i)
        for e in evs:
            if e.type == "full_charge":
                fired += 1
    assert fired == 1
    # kv must persist the timestamp for days_since computation
    assert det.KV_LAST_FULL_CHARGE in kv


def test_full_charge_does_not_refire_on_taper_current_wobble():
    """Production bug from 2026-06-08 01:56 soak: tapering current oscillated
    around the 5A threshold, causing the condition to flip false→true within
    20s and re-fire ``full_charge`` (two identical events stamped 20s apart).
    The detector must absorb brief wobbles."""
    fc = det.FullChargeDetector(kv={}, taper_a=5.0, rearm_seconds=300)

    def at(soc, v, current):
        return _bank(
            _pack(1, soc, v, current),
            _pack(2, soc, v, current),
            _pack(3, soc, v, current),
        )

    # all packs full, currents tapered → rising edge
    full = at(100, 14.25, 1.5)
    # transient wobble: one pack's current jumps to 6A briefly
    wobble = _bank(
        _pack(1, 100, 14.25, 1.5),
        _pack(2, 100, 14.25, 6.0),   # over taper threshold for one sample
        _pack(3, 100, 14.25, 1.5),
    )

    e1 = fc.evaluate(full, now=1000)
    assert [x.type for x in e1] == ["full_charge"]

    # wobble at t+10s → condition is false but we MUST NOT re-arm
    assert fc.evaluate(wobble, now=1010) == []
    # back to full at t+20s → MUST NOT re-fire (was_full still latched)
    assert fc.evaluate(full, now=1020) == []
    # several more taper messages, all full → still silent
    assert fc.evaluate(full, now=1050) == []
    assert fc.evaluate(full, now=1100) == []


def test_full_charge_refires_after_real_discharge_recycle():
    """After a full charge, the next discharge-and-recharge cycle should
    fire ``full_charge`` again. Re-arm requires sustained un-full for
    rearm_seconds (default 300s)."""
    fc = det.FullChargeDetector(kv={}, taper_a=5.0, rearm_seconds=300)

    def at(soc, v, current):
        return _bank(_pack(1, soc, v, current), _pack(2, soc, v, current), _pack(3, soc, v, current))

    full = at(100, 14.25, 1.5)
    discharging = at(70, 13.1, -25.0)  # solid discharge — clearly not-full

    # first calibration
    e1 = fc.evaluate(full, now=0)
    assert [x.type for x in e1] == ["full_charge"]

    # discharging starts; clock begins ticking at t=10s
    fc.evaluate(discharging, now=10)
    # still inside the re-arm window — even if it comes back to full briefly,
    # we'd treat it as latched. But here it stays discharging.
    fc.evaluate(discharging, now=200)
    # 5 minutes of sustained un-full → was_full clears
    fc.evaluate(discharging, now=400)

    # new rising edge after a real recharge cycle MUST fire again
    e2 = fc.evaluate(full, now=86_400)  # next day
    assert [x.type for x in e2] == ["full_charge"]


def test_full_charge_overdue_fires_at_most_once_per_day():
    kv = {det.KV_LAST_FULL_CHARGE: "0"}  # last full charge at epoch
    fc = det.FullChargeDetector(kv=kv, remind_after_days=30)
    bank = _bank(_pack(1, 60, 13.2, -2), _pack(2, 60, 13.2, -2))  # not full

    now = 31 * 86400  # 31 days later
    evs1 = fc.evaluate(bank, now=now)
    overdue = [e for e in evs1 if e.type == "full_charge_overdue"]
    assert len(overdue) == 1

    # again 1 second later: must not re-fire
    evs2 = fc.evaluate(bank, now=now + 1)
    assert [e for e in evs2 if e.type == "full_charge_overdue"] == []

    # again 25 hours later: may re-fire
    evs3 = fc.evaluate(bank, now=now + 25 * 3600)
    assert any(e.type == "full_charge_overdue" for e in evs3)


# ---------------------------------------------------------------------------
# CurrentShareDetector
# ---------------------------------------------------------------------------

def test_current_share_silent_when_bank_idle():
    cs = det.CurrentShareDetector(excess_pct=25, min_amps=10, sustain_seconds=300)
    bank = _bank(_pack(1, 80, 13.2, -1), _pack(2, 80, 13.2, -1), _pack(3, 80, 13.2, -1))
    assert cs.evaluate(bank, now=0) == []


def test_current_share_fires_when_one_pack_overworks():
    cs = det.CurrentShareDetector(excess_pct=25, min_amps=10, sustain_seconds=300)
    # 3 packs, expected share = 33.3%. Limit at +25% = ~41.7%.
    # pack 3 carries 50A vs 10A each on packs 1/2 → 71% share, way over.
    bank = _bank(_pack(1, 80, 13.2, -10), _pack(2, 80, 13.2, -10), _pack(3, 80, 13.2, -50))
    cs.evaluate(bank, now=0)
    cs.evaluate(bank, now=150)
    fired = cs.evaluate(bank, now=300)
    types = [e.type for e in fired]
    assert "current_share_imbalance" in types
    pack3_events = [e for e in fired if e.detail.get("pack") == 3]
    assert pack3_events


def test_current_share_silent_on_small_absolute_excess():
    # 2026-10-08 10:30: pack 3 at 6.25 A of a ~14 A bank = 44% share, over the
    # 41.7% limit, but only ~1.5 A above fair share — BMS resolution, not load.
    cs = det.CurrentShareDetector(excess_pct=25, min_amps=10, sustain_seconds=300)
    bank = _bank(_pack(1, 92, 13.3, -4.0), _pack(2, 92, 13.3, -3.9), _pack(3, 92, 13.3, -6.25))
    for t in (0, 150, 300, 600):
        assert cs.evaluate(bank, now=t) == []


def test_current_share_fires_on_large_absolute_excess_at_high_current():
    # 100 A bank, pack 3 at 45 A: 45% share and ~11.7 A over fair share.
    cs = det.CurrentShareDetector(excess_pct=25, min_amps=10, sustain_seconds=300)
    bank = _bank(_pack(1, 80, 13.2, -27.5), _pack(2, 80, 13.2, -27.5), _pack(3, 80, 13.2, -45))
    cs.evaluate(bank, now=0)
    fired = cs.evaluate(bank, now=300)
    assert [e.detail["pack"] for e in fired if e.type == "current_share_imbalance"] == [3]


def test_current_share_resets_when_idle_returns():
    cs = det.CurrentShareDetector(excess_pct=25, min_amps=10, sustain_seconds=300)
    busy = _bank(_pack(1, 80, 13.2, -10), _pack(2, 80, 13.2, -10), _pack(3, 80, 13.2, -50))
    idle = _bank(_pack(1, 80, 13.2, 0), _pack(2, 80, 13.2, 0), _pack(3, 80, 13.2, 0))
    cs.evaluate(busy, now=0)
    cs.evaluate(idle, now=150)        # idle resets the over_since clock
    cs.evaluate(busy, now=160)        # restarts the clock
    assert cs.evaluate(busy, now=300) == []   # only 140s elapsed since restart


# ---------------------------------------------------------------------------
# TemperatureDetector
# ---------------------------------------------------------------------------

def test_temp_high_edge_and_hysteresis():
    t = det.TemperatureDetector(high_c=45, low_c=2, hysteresis_c=2)
    cool = _bank(_pack(1, 80, 13.2, 0, temp=40))
    hot = _bank(_pack(1, 80, 13.2, 0, temp=46))
    warm = _bank(_pack(1, 80, 13.2, 0, temp=44))  # 44 > clear (43) → still alerted
    cool2 = _bank(_pack(1, 80, 13.2, 0, temp=42))  # ≤43 → clears

    assert t.evaluate(cool, now=0) == []
    fired = t.evaluate(hot, now=10)
    assert len(fired) == 1 and fired[0].type == "temp_high"
    # in hysteresis band: no event
    assert t.evaluate(warm, now=20) == []
    cleared = t.evaluate(cool2, now=30)
    assert len(cleared) == 1 and cleared[0].type == "temp_high_cleared"


def test_temp_low_fires_on_freeze_risk():
    t = det.TemperatureDetector(high_c=45, low_c=2)
    cold = _bank(_pack(1, 80, 13.2, 0, temp=1.0))
    fired = t.evaluate(cold, now=0)
    assert any(e.type == "temp_low" for e in fired)


# ---------------------------------------------------------------------------
# LowChargeDetector
# ---------------------------------------------------------------------------

def test_low_soc_warns_then_crit_escalation():
    lc = det.LowChargeDetector(low_soc_pct=15, crit_voltage=12.3)

    healthy = _bank(_pack(1, 50, 13.0, -1), _pack(2, 50, 13.0, -1))
    low = _bank(_pack(1, 14, 12.6, -1), _pack(2, 14, 12.6, -1))
    crit = _bank(_pack(1, 8, 12.2, -1), _pack(2, 8, 12.2, -1))

    assert lc.evaluate(healthy, now=0) == []
    warn_events = lc.evaluate(low, now=10)
    assert any(e.type == "low_soc" and e.severity == Severity.WARN for e in warn_events)
    crit_events = lc.evaluate(crit, now=20)
    assert any(e.type == "low_soc_crit" and e.severity == Severity.CRIT for e in crit_events)


def test_missing_pack_voltage_is_not_a_crit():
    """2026-10-29 17:24:56: pack 1 arrived with soc 99 and no voltage (parsed as
    0.0) as it dropped off the bus, and raised a CRIT that cleared 5 s later."""
    lc = det.LowChargeDetector(low_soc_pct=15, crit_voltage=12.3)
    full = _bank(_pack(1, 99, 13.3, 0), _pack(2, 99, 13.3, 0), _pack(3, 99, 13.3, 0))
    glitch = _bank(_pack(1, 99, 0.0, 0), _pack(2, 99, 13.3, 0), _pack(3, 99, 13.3, 0))
    assert lc.evaluate(full, now=0) == []
    assert lc.evaluate(glitch, now=5) == []
    assert lc.evaluate(full, now=10) == []


def test_real_low_voltage_still_crits_beside_a_missing_one():
    lc = det.LowChargeDetector(low_soc_pct=15, crit_voltage=12.3)
    bank = _bank(_pack(1, 8, 0.0, 0), _pack(2, 8, 12.2, -1))
    events = lc.evaluate(bank, now=0)
    crit = [e for e in events if e.type == "low_soc_crit"]
    assert crit and crit[0].detail["min_voltage"] == 12.2


def test_crit_holds_through_a_round_with_no_real_voltage():
    """No usable voltage says nothing either way: an active crit must neither
    clear nor re-fire."""
    lc = det.LowChargeDetector(low_soc_pct=15, crit_voltage=12.3)
    low = _bank(_pack(1, 8, 12.2, -1))
    blank = _bank(_pack(1, 8, 0.0, 0))
    assert any(e.type == "low_soc_crit" for e in lc.evaluate(low, now=0))
    assert not any(e.type.startswith("low_soc_crit") for e in lc.evaluate(blank, now=5))
    assert lc.crit_alerted


def test_low_soc_warn_with_no_real_voltage():
    lc = det.LowChargeDetector(low_soc_pct=15, crit_voltage=12.3)
    events = lc.evaluate(_bank(_pack(1, 10, 0.0, 0)), now=0)
    warn = [e for e in events if e.type == "low_soc"]
    assert warn and warn[0].detail["min_voltage"] is None


# ---------------------------------------------------------------------------
# CellImbalanceDetector (BLE-sourced)
# ---------------------------------------------------------------------------

def test_cell_imbalance_in_pack_fires_after_sustain():
    cd = det.CellImbalanceDetector(in_pack_threshold_mv=50, cross_pack_threshold_mv=100,
                                   sustain_seconds=60)
    # balanced — no event
    balanced = {1: [3330, 3330, 3330, 3330],
                2: [3340, 3340, 3340, 3340],
                3: [3325, 3325, 3325, 3325]}
    assert cd.evaluate(balanced, now=0) == []
    # pack 3 develops 60 mV in-pack spread; below sustain window
    drifting = {1: [3330, 3330, 3330, 3330],
                2: [3340, 3340, 3340, 3340],
                3: [3325, 3325, 3325, 3265]}  # 60 mV spread
    assert cd.evaluate(drifting, now=10) == []  # not enough sustained time
    # after sustain → fires
    events = cd.evaluate(drifting, now=80)
    assert any(e.type == "cell_imbalance" and e.detail["pack"] == 3 for e in events)
    # back to balanced → cleared
    cleared = cd.evaluate(balanced, now=100)
    assert any(e.type == "cell_imbalance_cleared" and e.detail["pack"] == 3 for e in cleared)


def test_cell_imbalance_cross_pack_fires_after_sustain():
    cd = det.CellImbalanceDetector(in_pack_threshold_mv=50, cross_pack_threshold_mv=100,
                                   sustain_seconds=60)
    # in-pack tight everywhere, but pack 3 sits 110 mV below pack 1
    drifting = {1: [3340, 3340, 3340, 3340],
                2: [3340, 3340, 3340, 3340],
                3: [3230, 3230, 3230, 3230]}
    assert cd.evaluate(drifting, now=0) == []
    events = cd.evaluate(drifting, now=80)
    assert any(e.type == "cell_imbalance_bank" for e in events)
    # back to balanced → cleared
    balanced = {1: [3340, 3340, 3340, 3340],
                2: [3340, 3340, 3340, 3340],
                3: [3338, 3338, 3338, 3338]}
    cleared = cd.evaluate(balanced, now=100)
    assert any(e.type == "cell_imbalance_bank_cleared" for e in cleared)


def test_cell_imbalance_hysteresis_prevents_flapping():
    cd = det.CellImbalanceDetector(in_pack_threshold_mv=50, cross_pack_threshold_mv=100,
                                   sustain_seconds=10, clear_hysteresis_mv=10)
    # 60 mV → fires
    drifting = {1: [3330, 3330, 3330, 3270]}  # spread 60
    cd.evaluate(drifting, now=0)
    events = cd.evaluate(drifting, now=20)
    assert any(e.type == "cell_imbalance" for e in events)
    # 45 mV — within hysteresis band, must NOT clear (clear threshold = 40)
    mid = {1: [3330, 3330, 3330, 3285]}  # spread 45
    cleared = cd.evaluate(mid, now=30)
    assert not any(e.type == "cell_imbalance_cleared" for e in cleared)
    # 35 mV — below clear threshold, fires cleared
    low = {1: [3330, 3330, 3330, 3295]}  # spread 35
    cleared = cd.evaluate(low, now=40)
    assert any(e.type == "cell_imbalance_cleared" for e in cleared)


def test_cell_imbalance_handles_empty_input():
    cd = det.CellImbalanceDetector()
    assert cd.evaluate({}, now=0) == []
    assert cd.evaluate({1: []}, now=0) == []


# ---------------------------------------------------------------------------
# SolarOutputOpenDetector
# ---------------------------------------------------------------------------

P = "N/0123456789ab"
SC = f"{P}/solarcharger/277"


def _solar_at(pv_power=200.0, pv_voltage=17.0, current=14.0, state=3, error=0):
    """A SolarState fed through the real topic parser, not hand-built."""
    from cerberus.solar import SolarState
    s = SolarState()
    s.update(f"{SC}/ErrorCode", error)
    s.update(f"{SC}/State", state)
    s.update(f"{SC}/Pv/V", pv_voltage)
    s.update(f"{SC}/Yield/Power", pv_power)
    s.update(f"{SC}/Dc/0/Current", current)
    return s


def _full_bank(soc: float = 60.0) -> BankState:
    return _bank(_pack(1, soc, 13.1, -1.0), _pack(2, soc, 13.1, -1.0),
                 _pack(3, soc, 13.1, -1.0))


def _load_solar_trip() -> list[dict]:
    rows = []
    with (FIXTURES / "solar_breaker_trip_2026_08_29.jsonl").open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def test_solar_open_fires_on_real_breaker_trip_fixture():
    """Replay of the 2026-08-29 09:27 trip: 199 W / 13.9 A -> 0 W with PV
    floating to 20.8 V open-circuit, then a manual reset 5 minutes later."""
    rows = _load_solar_trip()
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()

    fired: list = []
    cleared: list = []
    for r in rows:
        solar = _solar_at(r["pv_power"], r["pv_voltage"], r["charge_current"],
                          r["state"], r["error_code"])
        for ev in d.evaluate(solar, bank, r["ts"]):
            (fired if ev.type == "solar_output_open" else cleared).append(ev)

    assert len(fired) == 1, "exactly one alert per outage, not one per sample"
    assert fired[0].severity is det.Severity.WARN
    # trip at 1788010057, sustain 90s -> first eligible sample is 1788010147
    assert fired[0].ts == 1788010147
    assert fired[0].detail["pv_voltage"] == 20.7
    assert fired[0].detail["pv_power"] == 0.0

    assert len(cleared) == 1
    assert cleared[0].ts == 1788010359
    assert cleared[0].detail["outage_seconds"] == 302


def test_solar_open_does_not_fire_before_sustain_elapses():
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()
    tripped = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    assert d.evaluate(tripped, bank, 1000) == []
    assert d.evaluate(tripped, bank, 1060) == []
    assert [e.type for e in d.evaluate(tripped, bank, 1090)] == ["solar_output_open"]


def test_solar_open_silent_at_night():
    """Array dark reads ~0.4 V, not open-circuit. No sun is not a fault."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()
    night = _solar_at(pv_power=0.0, pv_voltage=0.4, current=0.0, state=0)
    for t in range(0, 1200, 60):
        assert d.evaluate(night, bank, t) == []


def test_solar_open_silent_when_bank_is_full():
    """A finished charge also sits at 0 W with PV at Voc. Only the bank
    distinguishes 'stopped on purpose' from 'path is broken'."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90, full_soc_guard_pct=97.0)
    full = _bank(_pack(1, 99.0, 13.6, 0.0), _pack(2, 99.0, 13.6, 0.0),
                 _pack(3, 98.0, 13.6, 0.0))
    solar = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=5)
    for t in range(0, 1200, 60):
        assert d.evaluate(solar, full, t) == []


def test_solar_open_fires_when_bank_state_unknown():
    """Absent bank data must not suppress — an unknown SoC is not a full one."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    solar = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    d.evaluate(solar, None, 0)
    assert [e.type for e in d.evaluate(solar, None, 100)] == ["solar_output_open"]


def test_solar_open_defers_to_a_real_mppt_error():
    """error_code != 0 means the controller sees the fault itself; that is a
    different condition and deserves its own alert, not this label."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()
    errored = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=2, error=67)
    for t in range(0, 1200, 60):
        assert d.evaluate(errored, bank, t) == []


def test_solar_open_ignores_partially_populated_tree():
    """Absent is not zero — a half-filled tree at startup is not a trip."""
    from cerberus.solar import SolarState
    d = det.SolarOutputOpenDetector(sustain_seconds=0)
    bank = _full_bank()
    s = SolarState()
    s.update(f"{SC}/Pv/V", 20.7)          # voltage only; no power, current, error
    assert d.evaluate(s, bank, 0) == []
    s.update(f"{SC}/Yield/Power", 0.0)
    assert d.evaluate(s, bank, 1) == []
    s.update(f"{SC}/Dc/0/Current", 0.0)
    assert d.evaluate(s, bank, 2) == [], "still no ErrorCode seen"
    s.update(f"{SC}/ErrorCode", 0)
    assert [e.type for e in d.evaluate(s, bank, 3)] == ["solar_output_open"]


def test_solar_open_no_data_is_silent():
    from cerberus.solar import SolarState
    d = det.SolarOutputOpenDetector(sustain_seconds=0)
    assert d.evaluate(SolarState(), _full_bank(), 0) == []


def test_solar_open_rearms_for_a_second_trip():
    """08-23 tripped twice in half an hour. The second must still alert."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()
    tripped = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    running = _solar_at(pv_power=200.0, pv_voltage=17.0, current=14.0, state=3)

    d.evaluate(tripped, bank, 0)
    assert [e.type for e in d.evaluate(tripped, bank, 100)] == ["solar_output_open"]
    assert [e.type for e in d.evaluate(running, bank, 200)] == ["solar_output_open_cleared"]
    d.evaluate(tripped, bank, 300)
    assert [e.type for e in d.evaluate(tripped, bank, 400)] == ["solar_output_open"]


def test_solar_open_brief_dropout_does_not_alert():
    """One zero-power sample between two healthy ones is noise, not a trip."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _full_bank()
    running = _solar_at(pv_power=200.0, pv_voltage=17.0, current=14.0, state=3)
    blip = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    assert d.evaluate(running, bank, 0) == []
    assert d.evaluate(blip, bank, 30) == []
    assert d.evaluate(running, bank, 60) == []
    assert d.evaluate(running, bank, 90) == []


def _bank_at(bank_a: float, soc: float = 88.0) -> BankState:
    """Three packs splitting bank_a evenly at the given SoC."""
    per = bank_a / 3.0
    return _bank(_pack(1, soc, 13.5, per), _pack(2, soc, 13.5, per),
                 _pack(3, soc + 1, 13.5, per))


def test_solar_open_silent_while_another_source_charges():
    """Replay of the 2026-08-29 14:25 FALSE POSITIVE. The alternator held the
    bus at ~13.5 V while charging at 125-179 A; the MPPT read that as a full
    battery, dropped to float at 0 W, and PV floated to 19.8-21.0 V — the exact
    trip signature. The shipped detector fired. It must not."""
    rows = []
    with (FIXTURES / "solar_float_false_positive_2026_08_29.jsonl").open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    assert any(r["bank_a"] > 100 for r in rows), "fixture should show heavy charging"
    assert any(r["pv_power"] == 0.0 and r["pv_voltage"] > 19 for r in rows)

    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    events = []
    for r in rows:
        solar = _solar_at(r["pv_power"], r["pv_voltage"], r["charge_current"],
                          r["state"], r["error_code"])
        events += d.evaluate(solar, _bank_at(r["bank_a"], r["min_soc"]), r["ts"])
    assert events == [], f"false positive while alternator charging: {events}"


def test_solar_open_still_fires_when_bank_is_discharging():
    """The real 09:27 and 09:41 trips ran at -8 to -13.5 A. The charging guard
    must not suppress those."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    bank = _bank_at(-13.5, 73.0)
    tripped = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    d.evaluate(tripped, bank, 0)
    assert [e.type for e in d.evaluate(tripped, bank, 100)] == ["solar_output_open"]


def test_solar_open_fires_when_only_solar_would_be_charging():
    """Guard sits above the array's ~15 A ceiling, so a trickle of charge from
    the array itself never masks a genuine open."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90, charging_guard_a=20.0)
    bank = _bank_at(12.0, 80.0)
    tripped = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    d.evaluate(tripped, bank, 0)
    assert [e.type for e in d.evaluate(tripped, bank, 100)] == ["solar_output_open"]


def test_solar_open_charging_guard_boundary():
    d = det.SolarOutputOpenDetector(sustain_seconds=0, charging_guard_a=20.0)
    tripped = _solar_at(pv_power=0.0, pv_voltage=20.7, current=0.0, state=4)
    assert d.evaluate(tripped, _bank_at(19.9, 80.0), 0) != []
    d2 = det.SolarOutputOpenDetector(sustain_seconds=0, charging_guard_a=20.0)
    assert d2.evaluate(tripped, _bank_at(20.0, 80.0), 0) == []


# ---------------------------------------------------------------------------
# SolarOutputOpenDetector — telling an open output from an absence of sun
#
# Up to 2026-09-18 this detector raised 61 alerts, and 50 of them fired between
# 06:30-06:50 or 18:20-19:34. They were tracking sunrise and sunset: the array's
# open-circuit voltage ramps through the 15-18 V band twice a day while it is
# still too dim to make power, and pv_voltage_min sat at 15 V. Replayed over the
# full 08-09 to 09-18 record, the old rule fires 132 times and the new one 6 —
# the three documented outage days (08-23, 08-24, 08-29) and nothing else.
#
# Voltages below are taken from those days.
# ---------------------------------------------------------------------------

def _sweep(d, bank, samples, t0=1000, step=30):
    """Feed (pv_power, pv_voltage) pairs 30 s apart; return the events raised."""
    events = []
    for n, (pw, pv) in enumerate(samples):
        cur = round(pw / 13.3, 2) if pw else 0.0
        solar = _solar_at(pv_power=pw, pv_voltage=pv, current=cur,
                          state=3 if pw else 4)
        events.extend(d.evaluate(solar, bank, t0 + n * step))
    return events


def test_solar_open_silent_through_the_dawn_ramp():
    """09-18 06:20-07:20: PV climbs 5 -> 18.7 V making nothing the whole way.

    The array is not open, it is not yet lit. Nothing has drawn from it, so
    there is no load to have come off.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    ramp = [(0.0, v) for v in (5.0, 8.5, 11.6, 13.1, 13.2, 13.5, 15.8, 16.7,
                               17.7, 17.8, 18.1, 18.3, 18.5)]
    assert _sweep(d, _full_bank(), ramp) == []


def test_solar_open_silent_through_dusk():
    """08-29 19:00-19:34: output decays to nothing and PV sinks with it."""
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    dusk = [(30.8, 17.2), (25.4, 17.0), (16.0, 16.8), (9.2, 16.6), (5.3, 16.5),
            (4.0, 16.5), (2.6, 16.0), (1.3, 15.8),
            # 08-30 19:05-19:14 — zero output sitting above 15 V for ten
            # minutes, which is what the old rule alerted on seven times that
            # evening alone.
            (0.0, 16.7), (0.0, 16.5), (0.0, 16.2), (0.0, 15.7), (0.0, 15.6),
            (0.0, 15.6), (0.0, 15.3), (0.0, 15.1),
            (0.0, 14.1), (0.0, 12.0)]
    assert _sweep(d, _full_bank(), dusk) == []


def test_solar_open_silent_on_a_passing_cloud():
    """08-27 13:10-14:25, which the voltage rule alerted on twice.

    Power collapses from 126 W to nothing — but PV falls *with* it, 16.6 down
    to 14.9. An array that has come off load goes the other way.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    cloud = [(97.0, 16.8), (126.5, 16.3), (64.2, 16.6), (28.1, 15.7),
             (24.0, 16.6), (37.7, 15.9), (13.2, 15.3), (1.3, 14.9),
             (0.0, 15.6), (0.0, 15.9), (0.0, 16.0), (0.0, 15.8)]
    assert _sweep(d, _full_bank(), cloud) == []


def test_solar_open_fires_when_the_array_comes_off_load():
    """The 08-29 09:27 trip in miniature: 201 W at 16.6 V, then 0 W at 20.7 V.

    Same zero power as the cloud above; opposite voltage. That is the whole
    discrimination.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    trip = [(201.1, 16.6), (199.6, 16.0), (198.0, 16.6),
            (0.0, 20.7), (0.0, 20.7), (0.0, 20.8), (0.0, 20.7)]
    types = [e.type for e in _sweep(d, _full_bank(), trip)]
    assert types == ["solar_output_open"]


def test_solar_open_fires_on_a_dim_day_below_the_old_threshold():
    """An overcast trip never reaches 20 V, and a fixed threshold would miss it.

    The array was working at 13.5 V, so 16 V is open circuit *for these
    conditions* — the reference is live, not a constant.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90)
    dim = [(60.0, 13.5), (55.0, 13.4), (58.0, 13.6),
           (0.0, 16.0), (0.0, 16.1), (0.0, 16.0), (0.0, 16.1)]
    types = [e.type for e in _sweep(d, _full_bank(), dim)]
    assert types == ["solar_output_open"]


def test_solar_open_re_raises_after_an_outage_outlasts_the_load_reference():
    """A trip left unreset overnight must alert again the next morning.

    By then nothing has drawn from the array for hours, so there is no loaded
    voltage to compare against and the open-circuit fallback carries it.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90, recent_window_seconds=600)
    # No production in living memory — history holds only zeros.
    morning = [(0.0, 20.1)] * 40
    types = [e.type for e in _sweep(d, _full_bank(), morning)]
    assert types == ["solar_output_open"]


def test_solar_open_does_not_self_clear_when_the_reference_ages_out():
    """Once raised, the outage ends when power returns — not when the
    evidence that started it falls out of the window.

    The history fills with zeros during a long outage, so the loaded-voltage
    reference disappears a few minutes in. Re-testing it on every sample would
    retract a live alert mid-outage and emit a false 'cleared'.
    """
    d = det.SolarOutputOpenDetector(sustain_seconds=90, recent_window_seconds=600)
    # Trip at open circuit, then hold for well past recent_window.
    trip = [(180.0, 16.5), (175.0, 16.4)] + [(0.0, 19.0)] * 60
    events = _sweep(d, _full_bank(), trip)
    assert [e.type for e in events] == ["solar_output_open"]

    # Power returns -> exactly one clear.
    back = _sweep(d, _full_bank(), [(150.0, 16.5)] * 3, t0=1000 + 62 * 30)
    assert [e.type for e in back] == ["solar_output_open_cleared"]


# ---------------------------------------------------------------------------
# Float guard (2026-09-20)
# ---------------------------------------------------------------------------

class _FloatGuardPrimary:
    def __init__(self, pv_voltage, state, error_code=0):
        self.pv_voltage, self.state, self.error_code = pv_voltage, state, error_code
    @property
    def state_label(self):
        return {0: "off", 3: "bulk", 4: "absorption", 5: "float"}.get(self.state, str(self.state))


class _FloatGuardSolar:
    has_data = True
    def __init__(self, w, a, primary):
        self.total_pv_power, self.total_charge_current, self.primary = w, a, primary


class _FloatGuardPack:
    def __init__(self, soc): self.soc = soc


class _FloatGuardBank:
    def __init__(self, soc, total):
        self.packs = {i: _FloatGuardPack(soc) for i in (1, 2, 3)}
        self.total_current = total


def _float_guard_detector(**kw):
    from cerberus.detectors import SolarOutputOpenDetector
    return SolarOutputOpenDetector(**kw)


def _drive(detector, idle_state, soc, ticks=12, idle_v=20.0, loaded_v=16.0):
    """10 min of real charging at Vmp, then the controller goes idle."""
    t = 1789800000
    for _ in range(20):
        detector.evaluate(_FloatGuardSolar(210, 15.8, _FloatGuardPrimary(loaded_v, 4)),
                          _FloatGuardBank(soc, -3.0), t)
        t += 30
    fired = []
    for _ in range(ticks):
        for e in detector.evaluate(_FloatGuardSolar(0.0, 0.0, _FloatGuardPrimary(idle_v, idle_state)),
                                   _FloatGuardBank(soc, -3.0), t):
            if e.type == "solar_output_open":
                fired.append(t)
        t += 30
    return fired


def test_legitimate_float_at_zero_watts_does_not_alert():
    """The case the 2026-09-19 rewire created. At 130 mOhm float still passed
    1-1.5 A / 10-15 W, above the 1 W trip threshold, so a true 0 W float was
    unreachable. At ~50 mOhm it is reachable, and it is byte-identical to an
    open output from the solar tree alone."""
    for soc in (80.0, 90.0, 96.0):
        assert not _drive(_float_guard_detector(), 5, soc), f"false positive at SoC {soc}"


def test_open_output_still_detected_in_every_other_state():
    """An open output cannot have completed an absorption cycle, so it never
    reports float. Both confirmed outages read as absorption: 08-23 10:42 and
    08-24 10:15, PV ~20 V at 0.0 W, state 4."""
    for state in (4, 3, 0):
        assert _drive(_float_guard_detector(), state, 85.0), f"missed a real trip in state {state}"


def test_float_guard_gates_the_rising_edge_only():
    """An outage that starts in absorption and later drifts into float keeps
    its alert. Gating the continuation would silently reopen the multi-hour
    gap this detector exists to close."""
    d = _float_guard_detector()
    assert _drive(d, 4, 85.0), "expected the initial alert"
    t = 1789800000 + 30 * 32
    cleared = []
    for _ in range(6):
        for e in d.evaluate(_FloatGuardSolar(0.0, 0.0, _FloatGuardPrimary(20.0, 5)),
                            _FloatGuardBank(85.0, -3.0), t):
            cleared.append(e.type)
        t += 30
    assert "solar_output_open_cleared" not in cleared, "drifting into float must not clear a live outage"


def test_float_state_is_configurable():
    d = _float_guard_detector(float_state=99)
    assert _drive(d, 5, 85.0), "state 5 should no longer be guarded when float_state=99"


# ---------------------------------------------------------------------------
# ColdChargeDetector
# ---------------------------------------------------------------------------

def _cold_bank(temp: float, amps_each: float, volts: float = 13.3) -> BankState:
    return _bank(_pack(1, 60, volts, amps_each, temp=12.0),
                 _pack(2, 60, volts, amps_each, temp=temp),
                 _pack(3, 60, volts, amps_each, temp=12.0))


def test_cold_charge_names_the_alternator_after_the_sustain():
    d = det.ColdChargeDetector(low_c=5, sustain_seconds=60)
    bank = _cold_bank(2.0, 30.0)                    # 90 A into the bank, 4 A of it solar
    assert d.evaluate(bank, 0, solar_a=4.0) == []
    (ev,) = d.evaluate(bank, 60, solar_a=4.0)
    assert ev.type == "cold_charge" and ev.detail == {
        "pack": 2, "temperature": 2.0, "current": 90.0, "source": "alternator"}
    assert d.evaluate(bank, 120, solar_a=4.0) == []           # once per episode


@pytest.mark.parametrize("solar_a, shore, source", [
    (9.0, False, "solar"), (0.0, True, "shore"), (None, False, "unknown")])
def test_cold_charge_source(solar_a, shore, source):
    d = det.ColdChargeDetector(sustain_seconds=0)
    (ev,) = d.evaluate(_cold_bank(1.0, 3.0), 0, solar_a=solar_a, shore=shore)
    assert ev.detail["source"] == source


@pytest.mark.parametrize("bank", [
    _cold_bank(2.0, -5.0),        # cold but discharging: temp_low's job, not this
    _cold_bank(2.0, 1.0),         # 3 A: under the charge threshold
    _cold_bank(8.0, 30.0),        # charging, but warm
    _cold_bank(0.0, 30.0, volts=0.0),  # missing readings parse as 0 V / 0 C
])
def test_cold_charge_stays_quiet(bank):
    d = det.ColdChargeDetector(sustain_seconds=0)
    assert d.evaluate(bank, 0, solar_a=0.0) == []


def test_cold_charge_clears_when_charging_stops_or_the_packs_warm():
    d = det.ColdChargeDetector(sustain_seconds=60)
    d.evaluate(_cold_bank(2.0, 30.0), 0, solar_a=0.0)
    assert d.evaluate(_cold_bank(2.0, 30.0), 60, solar_a=0.0)[0].type == "cold_charge"
    # A blip of no charge shorter than the sustain doesn't clear it.
    assert d.evaluate(_cold_bank(2.0, 0.0), 70, solar_a=0.0) == []
    assert d.evaluate(_cold_bank(2.0, 30.0), 80, solar_a=0.0) == []
    # Warming into the hysteresis band (5-7 C) doesn't either; 7 C does.
    assert d.evaluate(_cold_bank(6.0, 30.0), 200, solar_a=0.0) == []
    assert d.evaluate(_cold_bank(6.0, 30.0), 300, solar_a=0.0) == []
    d.evaluate(_cold_bank(7.0, 30.0), 400, solar_a=0.0)
    (ev,) = d.evaluate(_cold_bank(7.0, 30.0), 460, solar_a=0.0)
    assert ev.type == "cold_charge_cleared" and ev.severity == Severity.INFO
