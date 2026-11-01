"""Water-heater runs, seen as a step in AC load above the baseline.

Why this exists
---------------
The ~1.3 kW electric water heater is the largest load the inverter carries and
the one that raised the MultiPlus LowBattery pre-alarm on 2026-10-19. It runs
on its own thermostat, so it switches on whenever it likes, and each start is
a fresh test of the alarm. The 10-20 run measured ~4.2 mOhm between the packs
and the inverter terminals (0.55 V at 132 A), 2-4x a good install; every run
re-measures that, so a joint getting worse shows up without a planned test.

Detection
---------
The heater has no telemetry of its own. A run is an AC consumption sample more
than ON_DELTA_W above the baseline (median of the previous BASELINE_WINDOW_S of
ordinary load), and it ends at the first sample back under baseline +
OFF_DELTA_W. The baseline is frozen at the run's start, so the run itself never
drags it up. Any other kilowatt-class AC load (a kettle, a space heater) looks
the same; the event says "AC load run" in its detail for that reason, but
nothing else on this rig draws that much.

Each start emits ``heater_on`` and each end ``heater_run`` with the run's
figures. Both are info: the LowBattery alarm already has its own warn event,
so a run that saw one is warn only to put the alarm next to the run's
voltages. The run in progress lives in kv, so a collector restart mid-run
does not lose it.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import tzinfo

from .db import Db
from .location import local
from .events import Event, Severity, make

ON_DELTA_W = 800           # heater is ~1.3 kW; base load 100-200 W
OFF_DELTA_W = 400
BASELINE_WINDOW_S = 600
# Samples above this never count toward the baseline: it is ordinary load,
# not another big appliance.
BASELINE_MAX_W = 800
DEFAULT_BASELINE_W = 150.0
SAMPLE_S = 30              # ac_samples cadence
# A data gap this long inside a run ends it at the last sample: the collector
# was down, and the off edge is unknowable.
MAX_GAP_S = 300
# Path resistance needs real current: pack voltage is quantized to 0.05 V,
# which is 1 mOhm at 50 A.
PATH_MIN_A = 50.0
PACK_MATCH_S = 30          # battery and AC samples land on different seconds

KV_THROUGH = "heater.processed_through_ts"
KV_RUN = "heater.run"
EVENT_ON = "heater_on"
EVENT_RUN = "heater_run"


@dataclass(frozen=True)
class Settings:
    """Per-van values from config.yaml `heater`, sized to the appliance being
    tracked. The module constants above (this rig's 1.3 kW water heater) are
    the defaults."""
    on_delta_w: float = ON_DELTA_W
    off_delta_w: float = OFF_DELTA_W
    baseline_max_w: float = BASELINE_MAX_W
    default_baseline_w: float = DEFAULT_BASELINE_W
    # battery_samples rows per whole-bank sample; see drives.Settings.
    pack_count: int = 1
    # The van's zone for event times; None = host local. Set per tick.
    tz: tzinfo | None = None


DEFAULT_SETTINGS = Settings()


def _hhmm(ts: int, tz: tzinfo | None = None) -> str:
    return local(ts, tz).strftime("%H:%M")


def _pack(db: Db, ts: int, pack_count: int = 1) -> tuple[float, float] | None:
    """(mean pack V, mean SoC) at the nearest sample where every pack reported."""
    row = db.conn.execute(
        "SELECT AVG(voltage), AVG(soc) FROM battery_samples WHERE ts = ("
        "  SELECT ts FROM battery_samples WHERE ts BETWEEN ? AND ? "
        "  GROUP BY ts HAVING COUNT(*) = ? ORDER BY ABS(ts - ?) LIMIT 1)",
        (ts - PACK_MATCH_S, ts + PACK_MATCH_S, pack_count, ts),
    ).fetchone()
    return None if row is None or row[0] is None else (row[0], row[1])


def _alarms(db: Db, start: int, end: int) -> list[int]:
    """Timestamps of LowBattery raises during the run.

    One alarm arrives as two ids (vebus/276/LowBattery and .../L1/LowBattery)
    on the same second, so they are collapsed by timestamp. The window opens
    a sample early: the heater came on up to SAMPLE_S before the sample that
    caught it, and the 10-19 alarms fired within seconds of switch-on.
    """
    return [r[0] for r in db.conn.execute(
        "SELECT DISTINCT ts FROM events WHERE ts BETWEEN ? AND ? "
        "AND type LIKE 'alarm:%LowBattery' ORDER BY ts",
        (start - SAMPLE_S, end + SAMPLE_S),
    )]


def _new_run(db: Db, ts: int, w: float, base: float, iv, ia, pack_count: int = 1) -> dict:
    p = _pack(db, ts, pack_count)
    return {
        "start": ts, "last": ts, "base_w": round(base), "n": 1, "peak_w": w,
        "sum_w": w, "ah": 0.0, "ah_n": 0,
        "min_inv_v": iv, "start_inv_v": iv,
        "start_pack_v": None if p is None else round(p[0], 2),
        "start_soc": None if p is None else round(p[1], 1),
        "path": [],
    }


def _extend(run: dict, ts: int, w: float, iv, ia) -> None:
    run["last"] = ts
    run["n"] += 1
    run["peak_w"] = max(run["peak_w"], w)
    run["sum_w"] += w
    if iv is not None:
        run["min_inv_v"] = iv if run["min_inv_v"] is None else min(run["min_inv_v"], iv)


def _dc_sample(db: Db, run: dict, ts: int, iv, ia, pack_count: int = 1) -> None:
    """Ah and path resistance, from the inverter's own DC readings (since
    PR #13; earlier runs have neither)."""
    if ia is not None:
        run["ah"] += abs(ia) * SAMPLE_S / 3600
        run["ah_n"] += 1
    if iv is None or ia is None or abs(ia) < PATH_MIN_A:
        return
    p = _pack(db, ts, pack_count)
    if p is not None:
        run["path"].append((p[0] - iv) / abs(ia))


def summarize(db: Db, run: dict, end_ts: int, truncated: bool, pack_count: int = 1) -> dict:
    m = {
        "start_ts": run["start"],
        "end_ts": end_ts,
        "duration_min": round((end_ts - run["start"]) / 60, 1),
        "baseline_w": run["base_w"],
        "peak_w": round(run["peak_w"]),
        "avg_w": round(run["sum_w"] / run["n"]),
        "ah": round(run["ah"], 1) if run["ah_n"] else None,
        "start_inv_v": run["start_inv_v"] and round(run["start_inv_v"], 2),
        "min_inv_v": run["min_inv_v"] and round(run["min_inv_v"], 2),
        "start_pack_v": run["start_pack_v"],
        "start_soc": run["start_soc"],
        "truncated": truncated,
        "alarms": _alarms(db, run["start"], end_ts),
    }
    if run["path"]:
        m["path_mohm"] = round(statistics.median(run["path"]) * 1000, 1)
    end = _pack(db, end_ts, pack_count)
    if end is not None:
        m["end_soc"] = round(end[1], 1)
    return {k: v for k, v in m.items() if v is not None}


def summary_line(m: dict, tz: tzinfo | None = None) -> str:
    parts = [f"Heater {local(m['start_ts'], tz).strftime('%m-%d %H:%M')}",
             (f"{m['duration_min']:g} min" if m["duration_min"] >= 1 else "<1 min")
             + (" (data gap, end unknown)" if m["truncated"] else ""),
             f"peak {m['peak_w']} W"]
    if "ah" in m:
        parts.append(f"{m['ah']:.0f} Ah")
    if "min_inv_v" in m:
        parts.append(f"inverter min {m['min_inv_v']:.2f} V")
    if "path_mohm" in m:
        parts.append(f"path {m['path_mohm']:.1f} mOhm")
    if "start_soc" in m:
        soc = f"SoC {m['start_soc']:.0f}"
        if "end_soc" in m:
            soc += f"→{m['end_soc']:.0f}"
        parts.append(soc + "%")
    head = ", ".join(parts)
    return f"{head} — LowBattery x{len(m['alarms'])}" if m["alarms"] else f"{head} — no alarm"


def _on_event(run: dict, w: float, tz: tzinfo | None = None) -> Event:
    iv = run["start_inv_v"]
    summary = (f"Heater on {_hhmm(run['start'], tz)}: {w:.0f} W over a {run['base_w']} W baseline"
               + (f", inverter {iv:.2f} V" if iv is not None else "")
               + (f", SoC {run['start_soc']:.0f}%" if run["start_soc"] is not None else ""))
    return make(EVENT_ON, Severity.INFO, {
        "summary": summary, "start_ts": run["start"], "watts": round(w),
        "baseline_w": run["base_w"], "what": "AC load run",
    }, ts=run["start"])


def _run_event(m: dict, tz: tzinfo | None = None) -> Event:
    return make(EVENT_RUN, Severity.WARN if m["alarms"] else Severity.INFO,
                {"summary": summary_line(m, tz), "what": "AC load run", **m},
                ts=m["end_ts"])


def process(db: Db, now: int, settings: Settings = DEFAULT_SETTINGS) -> list[Event]:
    """Scan AC samples since the cursor; return heater_on / heater_run events.

    The first run starts the cursor at ``now``: past runs are not replayed.
    """
    through = db.get_kv(KV_THROUGH)
    if through is None:
        db.set_kv(KV_THROUGH, str(now))
        return []
    through = int(through)
    n, tz = settings.pack_count, settings.tz
    raw = db.get_kv(KV_RUN)
    run = json.loads(raw) if raw else None
    rows = db.conn.execute(
        "SELECT ts, consumption_w, inverter_dc_voltage, inverter_dc_current "
        "FROM ac_samples WHERE ts > ? AND ts <= ? AND consumption_w IS NOT NULL "
        "ORDER BY ts",
        (through - BASELINE_WINDOW_S, now),
    ).fetchall()
    events: list[Event] = []
    recent: list[tuple[int, float]] = []
    for ts, w, iv, ia in rows:
        if ts <= through:
            if run is None and w <= settings.baseline_max_w:
                recent.append((ts, w))
            continue
        if run is not None and ts - run["last"] > MAX_GAP_S:
            events.append(_run_event(summarize(db, run, run["last"], truncated=True, pack_count=n), tz))
            run = None
        if run is None:
            prior = [pw for pt, pw in recent if pt >= ts - BASELINE_WINDOW_S]
            base = statistics.median(prior) if prior else settings.default_baseline_w
            if w - base > settings.on_delta_w:
                run = _new_run(db, ts, w, base, iv, ia, n)
                _dc_sample(db, run, ts, iv, ia, n)
                events.append(_on_event(run, w, tz))
            elif w <= settings.baseline_max_w:
                recent.append((ts, w))
        elif w < run["base_w"] + settings.off_delta_w:
            events.append(_run_event(summarize(db, run, ts, truncated=False, pack_count=n), tz))
            run = None
            recent.append((ts, w))
        else:
            _extend(run, ts, w, iv, ia)
            _dc_sample(db, run, ts, iv, ia, n)
        through = ts
    db.set_kv(KV_THROUGH, str(through))
    db.set_kv(KV_RUN, json.dumps(run) if run else "")
    return events
