"""Mirror Victron alarm topics into Cerberus events.

The Cerbo publishes per-device alarms under topics like:
    N/<portal>/battery/<inst>/Alarms/<name>
    N/<portal>/vebus/<inst>/Alarms/<name>
    N/<portal>/vebus/<inst>/Alarms/L<phase>/<name>

Each payload is `{"value": <level>}` where level is `0` (ok), `1` (warning),
or `2` (alarm) on most Victron devices. We track each alarm by a stable id
derived from the topic (e.g. ``battery/1/HighVoltage``), emit a Cerberus
``Event`` on the **rising edge** (0 → >0) and a ``*_cleared`` event on the
**falling edge** (>0 → 0). Severity maps from level:

    1 → WARN
    2 → CRIT
    null / unknown → ignored (some vebus topics publish null when n/a)

The existing HA notify automation picks these up by listening on
``cerberus/events`` for severities ``warn`` and ``crit``, so a real Victron
alarm now lights up the user's phone the same way Cerberus's own detector
findings do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import Event, Severity, make as make_event


def parse_alarm_topic(topic: str) -> str | None:
    """Convert a full alarm topic to a stable, portal-independent id.

    Examples:
      N/<portal>/battery/1/Alarms/HighVoltage     -> battery/1/HighVoltage
      N/<portal>/vebus/276/Alarms/L1/Overload     -> vebus/276/L1/Overload
      N/<portal>/rvc/can0/Alarms/SameUniqueName   -> rvc/can0/SameUniqueName
    """
    parts = topic.split("/")
    if "Alarms" not in parts:
        return None
    idx = parts.index("Alarms")
    # need at least N/<portal>/<svc>/<inst>/Alarms/<name>
    if idx < 4 or idx == len(parts) - 1:
        return None
    svc_inst = "/".join(parts[2:idx])
    rest = "/".join(parts[idx + 1:])
    return f"{svc_inst}/{rest}"


@dataclass
class AlarmState:
    """In-memory state machine over the set of currently-active alarms.

    Keys are alarm ids returned by ``parse_alarm_topic``. Values are the last
    seen level. Edge transitions emit Cerberus events.
    """

    active: dict[str, int] = field(default_factory=dict)

    def update(self, alarm_id: str, level: int | None, ts: int) -> list[Event]:
        # Treat null/unknown as "no signal" — leave state alone, no event.
        if level is None:
            return []
        try:
            level = int(level)
        except (TypeError, ValueError):
            return []

        prev = self.active.get(alarm_id, 0)
        if level == prev:
            return []

        events: list[Event] = []

        if level > 0 and prev == 0:
            sev = Severity.CRIT if level >= 2 else Severity.WARN
            events.append(make_event(
                f"alarm:{alarm_id}",
                sev,
                {"alarm": alarm_id, "level": level},
                ts=ts,
            ))
        elif level == 0 and prev > 0:
            events.append(make_event(
                f"alarm:{alarm_id}_cleared",
                Severity.INFO,
                {"alarm": alarm_id, "prior_level": prev},
                ts=ts,
            ))
        # level increased without going through 0 (e.g. 1 → 2): emit escalation
        elif level > prev > 0:
            events.append(make_event(
                f"alarm:{alarm_id}",
                Severity.CRIT,
                {"alarm": alarm_id, "level": level, "prior_level": prev,
                 "note": "escalated without intermediate clear"},
                ts=ts,
            ))

        if level > 0:
            self.active[alarm_id] = level
        elif alarm_id in self.active:
            del self.active[alarm_id]
        return events
