"""The live snapshot: the collector's current view, for the UI.

The UI is a separate process with a read-only database connection (see
cerberus.ui), so it cannot see the collector's in-memory state. The collector
writes this JSON to kv every liveness tick (~5 s) instead. It is a *view*, not
a record: history stays in the sample tables, and nothing reads this back
except the UI.

Values the collector has not heard yet are null, never zero — the same
"absent is not zero" rule the detectors depend on.
"""

from __future__ import annotations

import json

from .ac import AcState
from .model import BankState
from .solar import SolarState

KV_SNAPSHOT = "live.snapshot"
# Bumped when the shape changes, so the UI can tell an old collector apart.
VERSION = 1


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def snapshot(
    now: int,
    cerbo_online: bool,
    bank: BankState | None,
    solar: SolarState,
    ac: AcState,
    capacity_fallback_ah: float | None = None,
) -> dict:
    out: dict = {"v": VERSION, "ts": now, "cerbo_online": bool(cerbo_online),
                 "bank": None, "solar": None, "ac": None}

    if bank is not None and bank.packs:
        packs = sorted(bank.packs.values(), key=lambda p: p.pack_id)
        caps = [p.capacity_ah for p in packs]
        # Reported capacity only counts when every pack reported it; a
        # partial sum would understate the bank.
        capacity = sum(caps) if all(c for c in caps) else capacity_fallback_ah
        out["bank"] = {
            "ts": bank.ts,
            "source": bank.source,
            # Mean of packs, as tools/status_report.py reports it.
            "soc": _r(sum(p.soc for p in packs) / len(packs), 1),
            "voltage": _r(sum(p.voltage for p in packs) / len(packs)),
            "current": _r(bank.total_current),
            "power": _r(bank.total_power, 0),
            "capacity_ah": _r(capacity, 0),
            "packs": [
                {"id": p.pack_id, "soc": _r(p.soc, 1), "voltage": _r(p.voltage),
                 "current": _r(p.current), "temperature_c": _r(p.temperature, 1)}
                for p in packs
            ],
        }

    if solar.has_data:
        out["solar"] = {
            "ts": solar.last_update_ts,
            "pv_w": _r(solar.total_pv_power, 0),
            "charge_a": _r(solar.total_charge_current),
            "state": solar.state_label,
            "yield_today_kwh": _r(solar.total_yield_today_kwh, 2),
            "max_power_today_w": _r(solar.max_power_today, 0),
            "error": solar.error_label if solar.has_error else None,
        }

    if ac.has_data:
        out["ac"] = {
            "ts": ac.last_update_ts,
            "consumption_w": _r(ac.total_consumption_w, 0),
            "input_w": _r(ac.total_input_w, 0),
            "shore_connected": ac.shore_connected,
            "inverter_state": ac.inverter_state_label,
            "charging_from_ac": ac.is_charging_from_ac,
        }
    return out


def dumps(snap: dict) -> str:
    return json.dumps(snap, separators=(",", ":"), sort_keys=True)
