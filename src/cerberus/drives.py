"""Per-drive segmentation, metrics and baseline comparison.

Why this exists
---------------
Every question this project has chased about charging (the 130 mOhm path, the
13.55 V alternator ceiling, the 09-24/26 overshoots, the instance-24 hot joint)
was settled by hand-picking drives and comparing them. This automates that:
after every drive, compute the same figures, store them, and compare each
against what prior drives under similar conditions showed. A drift — a joint
loosening again, the alternator weakening, a new trouble code — then shows up
as one drive standing out from its own history, instead of being found weeks
later by accident.

Two sources of drive boundaries
-------------------------------
* ``obd`` — engine_samples RPM > 0. Exact, but only exists once the adapter is
  fitted and only when the laptop rode along.
* ``electrical`` — net alternator current (bank current minus solar charge
  current) above 20 A. Works over all history back to 2026-06-07, so the
  baseline starts populated rather than empty. Two known blind spots: it
  cannot see the engine running into a full bank (current falls to ~0, so a
  drive fragments — 09-24 splits into six), and the shore charger looks the
  same, so a session only counts as a drive if it peaks above 100 A, which the
  ~60 A shore charger never does and the alternator always has.

Where OBD covers a drive it wins and overlapping electrical sessions are
dropped.

Anomaly rule
------------
Robust z-score against prior drives: (x - median) / (1.4826 * MAD), flagged at
|z| >= 3.5. Median/MAD rather than mean/SD so the drives that went wrong
(09-24) do not widen the baseline enough to hide the next one. Below
MIN_BASELINE comparable drives the metric is reported as "baseline forming"
and never flagged — a spurious alarm on drive three would teach you to ignore
the report.

Separately, a few absolute limits are flagged regardless of history, because
they are known-bad from this rig's own record rather than merely unusual.

Units follow the project convention: equipment in C, ambient air in F.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path

from .db import Db
from .location import day_start, local
from .events import Event, Severity, make

log = logging.getLogger(__name__)

# --- segmentation -----------------------------------------------------------
# Current thresholds scale with Settings.engine_charge_a, the most the engine
# can put into the bank. At this rig's 200 A: net engine current above 20 A
# counts as charging, a session must peak above 100 A to be a drive, and a
# 40 A change counts as a step (see Settings).
ENGINE_CHARGE_A = 200.0      # this rig: stock alternator, 140-220 A peaks
CHARGE_ON_FRAC, CHARGE_ON_FLOOR_A = 0.10, 5.0
DRIVE_PEAK_FRAC = 0.50
STEP_FRAC = 0.20
SHORE_INPUT_W = 50.0         # AC input above this while shore-connected = charger
MERGE_GAP_S = 600            # gaps up to this inside one drive (stops, Mac hiccups)
MIN_DRIVE_S = 180
OBD_GAP_S = 300              # RPM silence that ends an OBD drive
SETTLE_S = 900               # a drive is only final this long after it ended
SAMPLE_S = 30                # the collector's battery_samples cadence

# --- baseline ---------------------------------------------------------------
MIN_BASELINE = 8
Z_FLAG = 3.5
MIN_COVERAGE_PCT = 70.0      # drives with more missing data stay out of baselines

# --- absolute limits (known-bad on this rig) ---------------------------------
# Bank-side voltages are 12 V LiFePO4 defaults; config scales them through
# profile.VoltageProfile for other banks. The packs request 14.4 V; 09-24
# reached 14.75 V with two CRIT alarms.
ABS_PEAK_BANK_V = 14.6
# Time above this counts toward min_over_14_45 (the name is kept for stored
# history; on a 24/48 V bank the threshold is the scaled equivalent).
OVER_REQUEST_V = 14.45
# Defaults for Settings.joint_limit_c / joint_cold_c. On this rig the joint is
# temperature instance 24, on the alternator path. It reaches 50-64 C on every
# long post-rewire drive (64.3 C on 09-26), so heat alone is routine; the
# limit means "hotter than anything on record", not "warm".
ABS_JOINT_C = 70.0
# The joint rests at ~28-32 C. A drive starting hotter follows another drive
# and its rise says nothing about the joint, only about the gap between drives.
JOINT_COLD_C = 32.0
# Chassis (starter) battery while the engine runs, read by the ECU (PID 0142).
ABS_MODULE_V_LOW, ABS_MODULE_V_HIGH = 12.0, 15.0

# --- BMS SoC drift -------------------------------------------------------------
# The BMS SoC is a coulomb counter that only re-syncs at a true full charge,
# and this bank rarely gets one (early solar float, short drives). On 10-21 it
# sat pinned at 99% while the 13:22 drive put ~140 Ah into the bank at
# 13.6-13.8 V; true full came on the next drive, after a net +344 Ah since
# a "78%" morning that was really ~63%. A drive whose charge, as a share of
# capacity, outruns its SoC gain by this many points means every SoC figure
# until the next full charge reads high by about that much.
SOC_DRIFT_FLAG_PCT = 5.0
SOC_DRIFT_MIN_AH = 40.0      # below this the comparison is within SoC rounding
# Charge at or above this is absorption into a genuinely full bank, which
# legitimately raises SoC by nothing.
SOC_DRIFT_MAX_V = 14.2
# No fallback capacity: when neither the BMS nor bank.capacity_ah gives one,
# soc_drift_pct is skipped rather than computed against a guess. (This rig's
# 930 Ah, 3 x 310, is in config.yaml.)

# Hardware changes that make earlier drives incomparable. Drives before the
# latest epoch at or before a drive's start are excluded from its baseline.
# Per van, so none by default; set them in drives.baseline_epochs (this rig's
# 2026-09-19 DC rewire lives there).
DEFAULT_EPOCHS: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    """Per-van values from config.yaml `drives`. Defaults suit a van with no
    hot-joint probe: the joint metrics are simply not computed."""
    # Victron temperature instance of a probe on a charge-path joint, or None.
    joint_temp_instance: int | None = None
    joint_limit_c: float = ABS_JOINT_C
    joint_cold_c: float = JOINT_COLD_C
    # Bank voltages, from the van's profile.VoltageProfile.
    peak_limit_v: float = ABS_PEAK_BANK_V
    over_request_v: float = OVER_REQUEST_V
    bulk_max_v: float = SOC_DRIFT_MAX_V
    # battery_samples rows per timestamp that make a whole bank: the pack
    # count, or 1 for a single battery monitor.
    pack_count: int = 1
    # Most current the engine can put into the bank: typical alternator peak
    # when wired direct, the charger's rating behind a DC-DC.
    engine_charge_a: float = ENGINE_CHARGE_A
    # Used when the BMS does not report capacity.
    capacity_ah: float | None = None
    # The van's zone for report times and epoch dates; None = host local.
    # The collector sets it per tick (config, else the GX's own setting).
    tz: tzinfo | None = None

    @property
    def charge_on_a(self) -> float:
        return max(CHARGE_ON_FLOOR_A, CHARGE_ON_FRAC * self.engine_charge_a)

    @property
    def drive_peak_a(self) -> float:
        # Above the shore charger, below anything the engine does in bulk.
        return DRIVE_PEAK_FRAC * self.engine_charge_a

    @property
    def step_min_a(self) -> float:
        return STEP_FRAC * self.engine_charge_a


DEFAULT_SETTINGS = Settings()

KV_THROUGH = "drives.processed_through_ts"
EVENT_TYPE = "drive_report"

# Metrics compared against history, with a one-line meaning for the report.
# Each is either normalised already or only computed when conditions match
# (e.g. peak voltage only on drives that ended full), so drives are comparable.
BASELINE_METRICS: dict[str, str] = {
    "alt_bulk_a": "median net alternator current while SoC < 90%",
    "bank_step_mohm": "bank voltage change per amp across sudden current steps",
    "joint_c_per_ka2": "charge-path joint rise per kA² of mean I², cold starts",
    "peak_bank_v_full": "peak bank voltage, drives that ended full",
    "alt_bulk_a_idle": "median alternator current at idle, SoC < 90%",
    "alt_bulk_a_cruise": "median alternator current 1500-2500 rpm, SoC < 90%",
    "idle_fuel_lph": "median fuel rate at idle",
    "warmup_min": "minutes from cold (<40 C) to 70 C coolant",
    "module_v_max": "highest chassis-battery voltage while running",
}


@dataclass
class Drive:
    start_ts: int
    end_ts: int
    source: str
    metrics: dict = field(default_factory=dict)
    flags: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def electrical_series(db: Db, since: int, until: int,
                      pack_count: int = 1) -> list[tuple]:
    """(ts, bank_a, bank_v, soc, solar_a, alt_a, shore_charging) per sample.

    Only timestamps where every pack reported, because battery_samples is per
    pack and a partial row would understate bank current (by a third, on this
    rig's three packs).
    """
    rows = db.conn.execute(
        """
        WITH b AS (
          SELECT ts, SUM(current) i, AVG(voltage) v, AVG(soc) soc, COUNT(*) n
          FROM battery_samples
          WHERE source = 'mqtt' AND ts BETWEEN ? AND ?
          GROUP BY ts
        )
        SELECT b.ts, b.i, b.v, b.soc, s.charge_current, a.input_w, a.shore_connected
        FROM b
        LEFT JOIN solar_samples s ON s.ts = b.ts
        LEFT JOIN ac_samples a ON a.ts = b.ts
        WHERE b.n = ?
        ORDER BY b.ts
        """,
        (since, until, pack_count),
    ).fetchall()
    out = []
    for ts, i, v, soc, solar, input_w, shore in rows:
        if i is None:
            continue
        solar = solar or 0.0
        shore_charging = bool(shore == 1 and (input_w or 0) > SHORE_INPUT_W)
        out.append((ts, i, v, soc, solar, i - solar, shore_charging))
    return out


def electrical_sessions(series: list[tuple],
                        settings: Settings = DEFAULT_SETTINGS) -> list[tuple[int, int]]:
    """Alternator charge sessions in a series from electrical_series."""
    sessions: list[list] = []
    for ts, _i, _v, _soc, _solar, alt, shore in series:
        if alt <= settings.charge_on_a or shore:
            continue
        if sessions and ts - sessions[-1][1] <= MERGE_GAP_S:
            sessions[-1][1] = ts
            sessions[-1][2] = max(sessions[-1][2], alt)
        else:
            sessions.append([ts, ts, alt])
    return [
        (s, e) for s, e, peak in sessions
        if e - s >= MIN_DRIVE_S and peak >= settings.drive_peak_a
    ]


def obd_drives(db: Db, since: int, until: int) -> list[tuple[int, int]]:
    rows = db.conn.execute(
        "SELECT ts FROM engine_samples WHERE rpm > 0 AND ts BETWEEN ? AND ? "
        "ORDER BY ts",
        (since, until),
    ).fetchall()
    drives: list[list[int]] = []
    for (ts,) in rows:
        if drives and ts - drives[-1][1] <= OBD_GAP_S:
            drives[-1][1] = ts
        else:
            drives.append([ts, ts])
    return [(s, e) for s, e in drives if e - s >= MIN_DRIVE_S]


def find_drives(db: Db, since: int, until: int,
                settings: Settings = DEFAULT_SETTINGS) -> list[Drive]:
    obd = obd_drives(db, since, until)
    elec = electrical_sessions(
        electrical_series(db, since, until, settings.pack_count), settings)
    elec = [
        (s, e) for s, e in elec
        if not any(s <= oe and e >= os_ for os_, oe in obd)
    ]
    drives = [Drive(s, e, "obd") for s, e in obd]
    drives += [Drive(s, e, "electrical") for s, e in elec]
    return sorted(drives, key=lambda d: d.start_ts)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _integrate(points: list[tuple[int, float]], max_dt: int = 60) -> float:
    """Left Riemann sum in value·hours, not bridging gaps longer than max_dt."""
    total = 0.0
    for (t0, v0), (t1, _v1) in zip(points, points[1:]):
        dt = t1 - t0
        if 0 < dt <= max_dt:
            total += v0 * dt / 3600.0
    return total


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _obd_series(db: Db, name: str, start: int, end: int) -> list[tuple[int, float]]:
    return [
        (ts, v) for ts, v in db.conn.execute(
            "SELECT ts, value FROM obd_samples WHERE name = ? AND value IS NOT NULL "
            "AND ts BETWEEN ? AND ? ORDER BY ts",
            (name, start, end),
        )
    ]


def compute_metrics(db: Db, d: Drive, settings: Settings = DEFAULT_SETTINGS) -> dict:
    s, e = d.start_ts, d.end_ts
    m: dict = {
        "duration_min": round((e - s) / 60, 1),
    }
    series = electrical_series(db, s, e, settings.pack_count)
    m["coverage_pct"] = round(
        min(100.0, 100.0 * len(series) * SAMPLE_S / max(e - s, SAMPLE_S)), 1
    )
    if series:
        alt = [(ts, a) for ts, _i, _v, _soc, _so, a, _sh in series]
        m["alt_ah"] = _r(_integrate([(ts, max(a, 0.0)) for ts, a in alt]), 1)
        m["peak_alt_a"] = _r(max(a for _, a in alt), 1)
        m["soc_start"] = _r(series[0][3], 1)
        m["soc_end"] = _r(series[-1][3], 1)
        vs = [v for _ts, _i, v, *_ in series if v is not None]
        m["peak_bank_v"] = _r(max(vs)) if vs else None
        m["min_over_14_45"] = round(
            sum(1 for v in vs if v > settings.over_request_v) * SAMPLE_S / 60, 1
        )
        _soc_drift(db, d, m, series, settings)
        # "Ended full" by SoC is only true when SoC was honest: the drifted
        # 10-21 13:22 drive ended at "99%" with the bank far from full.
        if (m["soc_end"] is not None and m["soc_end"] >= 99
                and m.get("soc_drift_pct", 0) < SOC_DRIFT_FLAG_PCT):
            m["peak_bank_v_full"] = m["peak_bank_v"]
        bulk = [
            (i, v, a) for _ts, i, v, soc, _so, a, _sh in series
            if soc is not None and soc < 90 and a > settings.charge_on_a and v is not None
        ]
        m["alt_bulk_a"] = _r(_median([a for _i, _v, a in bulk]), 1)
        # Resistance from steps rather than a regression over the whole drive:
        # within a drive SoC climbs, so a fit of V on I mixes the pack's
        # voltage curve into the slope (it came out -0.6 to +1.9 mOhm across
        # comparable drives). Across one 30 s step SoC barely moves, so dV/dI
        # is the resistance the step saw — pack, wiring and short-term
        # polarisation together, consistently measured, which is what a
        # baseline needs.
        steps = []
        for a, b in zip(series, series[1:]):
            t0, i0, v0, soc0 = a[:4]
            t1, i1, v1 = b[:3]
            if v0 is None or v1 is None or t1 - t0 > SAMPLE_S + 5:
                continue
            if abs(i1 - i0) < settings.step_min_a:
                continue
            # Near full the BMS is throttling current itself and voltage
            # tracks its limit, not the resistance (09-24 gave 0.05 and -0.16).
            if soc0 is None or soc0 >= 95:
                continue
            steps.append((v1 - v0) / (i1 - i0))
        if len(steps) >= 5:
            m["bank_step_mohm"] = _r(_median(steps) * 1000, 2)
        m["bank_steps"] = len(steps)

    t24 = [] if settings.joint_temp_instance is None else [
        (ts, t) for ts, t in db.conn.execute(
            "SELECT ts, temperature_c FROM temperature_samples "
            "WHERE instance = ? AND temperature_c IS NOT NULL AND ts BETWEEN ? AND ? "
            "ORDER BY ts",
            (settings.joint_temp_instance, s, e),
        )
    ]
    if t24:
        m["joint_peak_c"] = _r(max(t for _, t in t24), 1)
        m["joint_rise_c"] = _r(max(t for _, t in t24) - t24[0][1], 1)
        # Joint heating scales with I² (that is how instance 24 was placed on
        # the alternator path), so normalise the rise by mean I² to compare a
        # 60 A drive with a 200 A one. Only meaningful once the joint has had
        # time to approach equilibrium.
        # Also only on drives that spent real time in bulk: a long drive into a
        # full bank has a tiny mean I² and the ratio explodes (09-24 11:06
        # gave 1397 against ~1050 for comparable drives).
        if (series and (e - s) >= 30 * 60 and t24[0][1] <= settings.joint_cold_c
                and m.get("alt_bulk_a") is not None):
            mean_i2 = statistics.fmean(max(a, 0.0) ** 2 for *_x, a, _sh in series)
            if mean_i2 > 0:
                m["joint_c_per_ka2"] = _r(m["joint_rise_c"] / (mean_i2 / 1e6), 1)

    wx = db.conn.execute(
        "SELECT AVG(air_temp_f) FROM weather_samples WHERE ts BETWEEN ? AND ?",
        (s - 1800, e + 1800),
    ).fetchone()
    if wx and wx[0] is not None:
        m["weather_air_f"] = _r(wx[0], 1)

    _obd_metrics(db, d, m, series, settings)
    return {k: v for k, v in m.items() if v is not None}


def _bank_capacity(db: Db, s: int, e: int, fallback: float | None = None) -> float | None:
    row = db.conn.execute(
        "SELECT SUM(cap) FROM (SELECT MAX(capacity_ah) cap FROM battery_samples "
        "WHERE pack_id > 0 AND capacity_ah > 0 AND ts BETWEEN ? AND ? "
        "GROUP BY pack_id)",
        (s, e),
    ).fetchone()
    return row[0] if row and row[0] else fallback


def _soc_drift(db: Db, d: Drive, m: dict, series: list[tuple],
               settings: Settings = DEFAULT_SETTINGS) -> None:
    """Net charge into the bank against what the BMS SoC admits to.

    soc_drift_pct > 0: the SoC rose less than the charge says it should, so
    the BMS was reading high. Only positive drift is meaningful; a negative
    one is usually the BMS jumping to 100% when it re-syncs at full.
    """
    if m.get("soc_start") is None or m.get("soc_end") is None:
        return
    ah = _integrate([(ts, i if v is not None and v < settings.bulk_max_v else 0.0)
                     for ts, i, v, *_ in series])
    m["bank_ah"] = _r(ah, 1)
    cap = _bank_capacity(db, d.start_ts, d.end_ts, settings.capacity_ah)
    if cap and ah >= SOC_DRIFT_MIN_AH:
        m["soc_drift_pct"] = _r(100.0 * ah / cap - (m["soc_end"] - m["soc_start"]), 1)


def _obd_metrics(db: Db, d: Drive, m: dict, series: list[tuple],
                 settings: Settings = DEFAULT_SETTINGS) -> None:
    s, e = d.start_ts, d.end_ts
    eng = db.conn.execute(
        "SELECT ts, rpm, speed_mph, coolant_c FROM engine_samples "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (s, e),
    ).fetchall()
    if not eng:
        return
    running = [r for r in eng if r[1] and r[1] > 0]
    if running:
        m["engine_min"] = round((running[-1][0] - running[0][0]) / 60, 1)
    m["distance_mi"] = _r(_integrate(
        [(ts, sp) for ts, _r_, sp, _c in eng if sp is not None], max_dt=10
    ), 1)
    idle = [r for r in running if (r[2] or 0) < 1]
    m["idle_pct"] = _r(100.0 * len(idle) / len(running), 0) if running else None
    moving = [r[1] for r in running if (r[2] or 0) >= 1]
    m["rpm_mean_moving"] = _r(statistics.fmean(moving), 0) if moving else None
    m["rpm_max"] = _r(max(r[1] for r in running), 0) if running else None
    coolant = [(ts, c) for ts, _r_, _sp, c in eng if c is not None]
    if coolant:
        m["coolant_max_c"] = _r(max(c for _, c in coolant), 0)
        if coolant[0][1] < 40:
            warm = next((ts for ts, c in coolant if c >= 70), None)
            if warm is not None:
                m["warmup_min"] = _r((warm - coolant[0][0]) / 60, 1)

    # Alternator output against RPM, inside bulk. The pairing that RPM was
    # fitted to answer: is it running hard, or just running.
    rpm_at = {ts: rpm for ts, rpm, _sp, _c in eng}
    def near_rpm(ts):
        for dt in (0, -1, 1, -2, 2):
            if ts + dt in rpm_at:
                return rpm_at[ts + dt]
        return None
    idle_a, cruise_a = [], []
    for ts, _i, _v, soc, _so, a, _sh in series:
        if soc is None or soc >= 90 or a <= settings.charge_on_a:
            continue
        rpm = near_rpm(ts)
        if rpm is None:
            continue
        if 0 < rpm < 1000:
            idle_a.append(a)
        elif 1500 <= rpm <= 2500:
            cruise_a.append(a)
    m["alt_bulk_a_idle"] = _r(_median(idle_a), 1)
    m["alt_bulk_a_cruise"] = _r(_median(cruise_a), 1)

    fuel = _obd_series(db, "fuel_rate_lph", s, e)
    if fuel:
        m["fuel_l"] = _r(_integrate(fuel, max_dt=30), 2)
        idle_ts = {r[0] for r in idle}
        m["idle_fuel_lph"] = _r(_median([v for ts, v in fuel if ts in idle_ts]), 2)
    mv = [v for _ts, v in _obd_series(db, "module_voltage_v", s, e)]
    if mv:
        m["module_v_min"] = _r(min(mv))
        m["module_v_max"] = _r(max(mv))
    amb = [v for _ts, v in _obd_series(db, "ambient_air_f", s, e)]
    if amb:
        m["ambient_air_f"] = _r(_median(amb), 1)

    dtc = db.conn.execute(
        "SELECT codes FROM obd_dtcs WHERE ts BETWEEN ? AND ? AND codes IS NOT NULL",
        (s - OBD_GAP_S, e),
    ).fetchall()
    if dtc:
        m["dtcs"] = sorted({c for (codes,) in dtc for c in codes.split(",") if c})


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def epoch_start(before_ts: int, epochs=DEFAULT_EPOCHS, tz: tzinfo | None = None) -> int:
    """Start of the hardware epoch containing before_ts (0 if none)."""
    starts = [day_start(e, tz) for e in epochs]
    return max((t for t in starts if t <= before_ts), default=0)


def prior_drives(db: Db, before_ts: int, epochs=DEFAULT_EPOCHS,
                 tz: tzinfo | None = None) -> list[dict]:
    """Metrics of earlier drives in the same hardware epoch."""
    out = []
    for (metrics,) in db.conn.execute(
        "SELECT metrics FROM drives WHERE start_ts >= ? AND start_ts < ? "
        "ORDER BY start_ts",
        (epoch_start(before_ts, epochs, tz), before_ts),
    ):
        try:
            out.append(json.loads(metrics))
        except ValueError:
            continue
    return out


def robust_z(x: float, history: list[float]) -> tuple[float, float, float]:
    """(z, median, scaled MAD). A zero MAD is floored at 2% of the median so a
    perfectly flat history does not turn every tiny change into infinity."""
    med = statistics.median(history)
    mad = statistics.median(abs(h - med) for h in history) * 1.4826
    mad = max(mad, abs(med) * 0.02, 1e-9)
    return (x - med) / mad, med, mad


def evaluate(d: Drive, history: list[dict],
             settings: Settings = DEFAULT_SETTINGS) -> list[dict]:
    """Flags for one drive. Each flag: kind, metric, value, and context."""
    m = d.metrics
    flags: list[dict] = []
    usable = [h for h in history if h.get("coverage_pct", 0) >= MIN_COVERAGE_PCT]
    for name in BASELINE_METRICS:
        x = m.get(name)
        if x is None:
            continue
        hist = [h[name] for h in usable if h.get(name) is not None]
        if len(hist) < MIN_BASELINE:
            continue
        z, med, _mad = robust_z(float(x), hist)
        if abs(z) >= Z_FLAG:
            flags.append({"kind": "anomaly", "metric": name, "value": x,
                          "baseline": round(med, 2), "z": round(z, 1),
                          "n": len(hist)})

    def limit(metric, value, cond, text):
        if value is not None and cond:
            flags.append({"kind": "limit", "metric": metric, "value": value,
                          "limit": text})
    limit("peak_bank_v", m.get("peak_bank_v"),
          (m.get("peak_bank_v") or 0) >= settings.peak_limit_v,
          f">= {settings.peak_limit_v} V")
    limit("joint_peak_c", m.get("joint_peak_c"),
          (m.get("joint_peak_c") or 0) >= settings.joint_limit_c,
          f">= {settings.joint_limit_c} C")
    limit("module_v_min", m.get("module_v_min"),
          m.get("module_v_min") is not None and m["module_v_min"] < ABS_MODULE_V_LOW,
          f"< {ABS_MODULE_V_LOW} V")
    limit("module_v_max", m.get("module_v_max"),
          m.get("module_v_max") is not None and m["module_v_max"] > ABS_MODULE_V_HIGH,
          f"> {ABS_MODULE_V_HIGH} V")

    drift = m.get("soc_drift_pct")
    if drift is not None and drift >= SOC_DRIFT_FLAG_PCT:
        flags.append({"kind": "soc_drift", "metric": "soc_drift_pct", "value": drift,
                      "bank_ah": m.get("bank_ah"),
                      "soc_gain": _r(m["soc_end"] - m["soc_start"], 1)})

    if m.get("dtcs"):
        seen = {c for h in history for c in h.get("dtcs", [])}
        new = [c for c in m["dtcs"] if c not in seen]
        if new:
            flags.append({"kind": "dtc", "metric": "dtcs", "value": new})
    return flags


def baseline_status(history: list[dict]) -> dict[str, int]:
    usable = [h for h in history if h.get("coverage_pct", 0) >= MIN_COVERAGE_PCT]
    return {
        name: sum(1 for h in usable if h.get(name) is not None)
        for name in BASELINE_METRICS
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _when(ts: int, tz: tzinfo | None = None) -> str:
    return local(ts, tz).strftime("%Y-%m-%d %H:%M")


def summary_line(d: Drive, tz: tzinfo | None = None) -> str:
    m = d.metrics
    parts = [f"Drive {_when(d.start_ts, tz)[5:]}", f"{m['duration_min']:.0f} min"]
    if "distance_mi" in m:
        parts.append(f"{m['distance_mi']:.0f} mi")
    if "alt_ah" in m:
        parts.append(f"+{m['alt_ah']:.0f} Ah")
    if "peak_bank_v" in m:
        parts.append(f"peak {m['peak_bank_v']:.2f} V")
    head = ", ".join(parts)
    if not d.flags:
        return f"{head} — normal"
    bits = []
    for f in d.flags[:3]:
        if f["kind"] == "anomaly":
            bits.append(f"{f['metric']} {f['value']} vs {f['baseline']} (z={f['z']})")
        elif f["kind"] == "limit":
            bits.append(f"{f['metric']} {f['value']} {f['limit']}")
        elif f["kind"] == "soc_drift":
            bits.append(f"BMS SoC reads ~{f['value']:.0f} pts high")
        else:
            bits.append(f"new codes {', '.join(f['value'])}")
    more = f" +{len(d.flags) - 3} more" if len(d.flags) > 3 else ""
    return f"{head} — {len(d.flags)} flag(s): " + "; ".join(bits) + more


def render_report(d: Drive, history: list[dict], tz: tzinfo | None = None) -> str:
    m = d.metrics
    lines = [
        f"# Drive {_when(d.start_ts, tz)} → {_when(d.end_ts, tz)[11:]}",
        "",
        summary_line(d, tz),
        "",
        f"Boundaries from **{d.source}**"
        + ("" if d.source == "obd" else
           " (alternator current; engine time running into a full bank is invisible)")
        + f". Data coverage {m.get('coverage_pct', 0):.0f}%"
        + (" — below the baseline threshold, so this drive is not used as history."
           if m.get("coverage_pct", 0) < MIN_COVERAGE_PCT else "."),
        "",
    ]
    if d.flags:
        lines += ["## Flags", ""]
        for f in d.flags:
            if f["kind"] == "anomaly":
                lines.append(
                    f"- **{f['metric']}** = {f['value']} against a baseline median of "
                    f"{f['baseline']} over {f['n']} drives (robust z {f['z']}). "
                    f"{BASELINE_METRICS[f['metric']]}."
                )
            elif f["kind"] == "limit":
                lines.append(f"- **{f['metric']}** = {f['value']}, limit {f['limit']}.")
            elif f["kind"] == "soc_drift":
                lines.append(
                    f"- **BMS SoC reads high:** {f['bank_ah']} Ah went into the bank "
                    f"but SoC rose only {f['soc_gain']} points, {f['value']} points "
                    "short. SoC figures read high by about that much until the next "
                    "full charge (packs at 14.4 V) re-syncs the BMS."
                )
            else:
                lines.append(f"- **New trouble codes:** {', '.join(f['value'])}.")
        lines.append("")
    lines += ["## Metrics", "", "| metric | value | baseline median | z | n |",
              "|---|---|---|---|---|"]
    usable = [h for h in history if h.get("coverage_pct", 0) >= MIN_COVERAGE_PCT]
    for k in sorted(m):
        v = m[k]
        med = z = n = ""
        if k in BASELINE_METRICS:
            hist = [h[k] for h in usable if h.get(k) is not None]
            n = str(len(hist))
            if len(hist) >= MIN_BASELINE:
                zz, mm, _ = robust_z(float(v), hist)
                med, z = f"{mm:.2f}", f"{zz:+.1f}"
            else:
                med = "forming"
        val = ", ".join(v) if isinstance(v, list) else v
        lines.append(f"| {k} | {val} | {med} | {z} | {n} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def record(db: Db, d: Drive, report_dir: Path | None,
           epochs=DEFAULT_EPOCHS, settings: Settings = DEFAULT_SETTINGS) -> None:
    history = prior_drives(db, d.start_ts, epochs, settings.tz)
    d.metrics = compute_metrics(db, d, settings)
    d.flags = evaluate(d, history, settings)
    path = None
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"drive-{local(d.start_ts, settings.tz):%Y%m%d-%H%M}.md"
        path.write_text(render_report(d, history, settings.tz))
    db.conn.execute(
        "INSERT OR REPLACE INTO drives "
        "(start_ts, end_ts, source, metrics, flags, report_path, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (d.start_ts, d.end_ts, d.source, json.dumps(d.metrics, sort_keys=True),
         json.dumps(d.flags), None if path is None else str(path), int(time.time())),
    )


def to_event(d: Drive, tz: tzinfo | None = None) -> Event:
    return make(EVENT_TYPE, Severity.WARN if d.flags else Severity.INFO, {
        "summary": summary_line(d, tz),
        "start_ts": d.start_ts,
        "flags": len(d.flags),
    }, ts=d.end_ts)


def process(db: Db, now: int, report_dir: Path | None,
            since: int | None = None, epochs=DEFAULT_EPOCHS,
            settings: Settings = DEFAULT_SETTINGS) -> list[Event]:
    """Record every finished drive not yet recorded; return events for them.

    First run (no cursor) backfills all history silently: four months of
    drives arriving as phone notifications at once would be noise. After that,
    each newly finished drive yields one drive_report event.
    """
    through = db.get_kv(KV_THROUGH)
    first_run = through is None and since is None
    start = since if since is not None else (0 if through is None
                                             else int(through) - 86400)
    events: list[Event] = []
    # Matched on overlap, not start_ts: a drive straddling the scan window's
    # start comes back with a truncated start and would otherwise be recorded
    # twice.
    known = db.conn.execute(
        "SELECT start_ts, end_ts FROM drives WHERE end_ts >= ?", (start,)
    ).fetchall()
    pending = False
    for d in find_drives(db, start, now, settings):
        if d.end_ts > now - SETTLE_S:
            pending = True   # still running, or might be — wait
            break
        if any(d.start_ts <= ke and d.end_ts >= ks for ks, ke in known):
            continue
        record(db, d, report_dir, epochs, settings)
        known.append((d.start_ts, d.end_ts))
        if not first_run:
            events.append(to_event(d, settings.tz))
        db.set_kv(KV_THROUGH, str(d.end_ts))
    # Nothing in flight: move the cursor up so a long parked stretch does not
    # widen every later scan. The 1-day lookback still covers any drive that
    # started before this point and has not finished.
    if not pending:
        db.set_kv(KV_THROUGH, str(now - SETTLE_S))
    return events


def rebuild(db: Db, now: int, report_dir: Path | None,
            epochs=DEFAULT_EPOCHS, settings: Settings = DEFAULT_SETTINGS) -> int:
    """Forget every recorded drive and recompute from scratch, silently.

    For after a metric or epoch change: baselines are built from stored
    metrics, so old rows computed the old way would otherwise linger.
    """
    db.conn.execute("DELETE FROM drives")
    db.conn.execute("DELETE FROM kv WHERE key = ?", (KV_THROUGH,))
    process(db, now, report_dir, epochs=epochs, settings=settings)
    return db.conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0]
