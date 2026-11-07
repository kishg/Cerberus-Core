"""Vehicle values imported through the Home Assistant broker.

Some vans report chassis data that neither the Cerbo nor the OBD port gives
while parked: the chassis maker's own cloud (Mercedes me on this van, read by
Home Assistant) knows tyre pressures, the starter battery's state, fluid
warnings, and fuel, AdBlue and odometer as of the last park. Home Assistant
republishes a snapshot on its broker, and Cerberus records it here.

The payload is generic, so nothing in this module knows the chassis maker:

    {"ts": 1793574274, "source": "mercedes_me",
     "values": {"fuel_level_pct": 65, "tire_fl_psi": 55, ...}}

Names Cerberus understands (anything else is stored and otherwise ignored):
fuel_level_pct, def_level_pct, odometer_mi, range_mi, tire_{fl,fr,rl,rr}_psi,
tire_warning, starter_battery_state, coolant_low, brake_fluid_low,
washer_low, ignition_state, lock_state, engine_light, service_due_mi (miles to
the next service), trip_mi, trip_mpg, trip_time_min (the trip computer since
it last reset, which can span several drives). Flags are 0/1.

starter_battery_state follows the Mercedes coding (the only source so far):
0 green, 4 green-yellow, 1 yellow, 3 orange, 2 red.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger(__name__)

# A snapshot claiming a time this far from now is clock trouble; use now.
MAX_SKEW_S = 3600


def parse_snapshot(payload: bytes | str, now: int | None = None
                   ) -> tuple[int, str, dict[str, float]] | None:
    """(ts, source, values) from a snapshot, or None if it isn't one.
    Non-numeric values are dropped, not guessed at."""
    now = int(time.time()) if now is None else now
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("values"), dict):
        return None
    source = str(d.get("source") or "import").strip() or "import"
    try:
        ts = int(d.get("ts") or now)
    except (TypeError, ValueError):
        ts = now
    if abs(ts - now) > MAX_SKEW_S:
        ts = now
    values: dict[str, float] = {}
    for k, v in d["values"].items():
        if isinstance(v, bool):
            values[str(k)] = float(v)
        elif isinstance(v, (int, float)):
            values[str(k)] = float(v)
    return ts, source, values
