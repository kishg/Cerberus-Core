"""Idle shut-off: put the inverter in charger-only when the AC load is idle.

The inverter's own cost while on is small (cerberus docs: ~9-20 W), but what
it powers when nobody is using anything (Starlink, chargers left plugged in)
is 95-150 W, about 270 Ah a day. This watches the AC load and, once it has
stayed under ``threshold_w`` for ``minutes``, switches the MultiPlus to
charger-only (VE.Bus Mode 1). Charger-only keeps shore charging and AC
pass-through working; it only stops inverting from the batteries.

**The first thing Cerberus writes to the Victron system.** Everything else is
read-only. Writes are limited to ``vebus/<inst>/Mode`` = 1 or 3, only over the
local Cerbo connection (never the VRM cloud relay), and every write is
confirmed by reading the mode back. Off by default.

**What turns it back on.** With the inverter in charger-only there is no AC
output to watch, so Cerberus can't tell when AC is wanted again. It turns the
inverter back on (Mode 3) when:

* the owner presses "Turn inverter on" in the UI (a request file beside
  config.yaml, as cerberus.phonefix does for the phone's position); or
* the morning time (``morning_on``) has passed since the shut-off.

A mode changed by hand (the Cerbo, the remote panel, VictronConnect, HA) is
always respected: it clears the shut-off and Cerberus doesn't touch the
inverter again until the load next goes idle. After any switch back on, idle
shut-off waits ``hold_minutes`` before it may act again, so the morning turn-on
or a button press isn't undone 15 minutes later by a quiet Starlink.

Never acts while: shore power is connected, the inverter isn't inverting,
inside the ``quiet_start``-``quiet_end`` window (van local time), the local
Cerbo connection is down, or a previous write is still unconfirmed.

decide() is pure: the collector feeds it the current readings and carries the
returned state (persisted in kv, so a restart keeps "we switched it off").
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time as dtime, timedelta, tzinfo

from pathlib import Path

from .events import Event, Severity, make
from .location import local

KV_STATE = "inverter_idle.state"
# The UI's request to turn the inverter back on, beside config.yaml.
REQUEST_FILE = ".cerberus-inverter-on.json"

# VE.Bus Mode values (vebus/<inst>/Mode). Only these two are ever written.
MODE_CHARGER_ONLY = 1
MODE_INVERTER_ONLY = 2
MODE_ON = 3
MODE_OFF = 4
WRITABLE_MODES = frozenset({MODE_CHARGER_ONLY, MODE_ON})
STATE_INVERTING = 9

THRESHOLD_W = 150.0     # above Starlink alone (~95-130 W floor), below Starlink + TV
MINUTES = 15.0
HOLD_MINUTES = 60.0
MORNING_ON = "07:00"
REQUEST_FRESH_SECONDS = 600  # a button press older than this is dropped, not acted on
CONFIRM_SECONDS = 60    # a write not read back by then is reported as failed
RETRY_SECONDS = 300     # floor between writes, so a refused write can't loop

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value, name: str) -> str | None:
    """'7:00' / '07:00' -> '07:00'; empty -> None (off). ValueError otherwise."""
    if value is None or str(value).strip() == "":
        return None
    m = _HHMM.match(str(value).strip())
    if not m:
        raise ValueError(f"{name}: expected a time like 07:00, got {value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _t(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


@dataclass(frozen=True)
class Settings:
    """config.yaml `inverter_idle`; editable in the UI's Settings."""
    enabled: bool = False
    threshold_w: float = THRESHOLD_W
    minutes: float = MINUTES
    hold_minutes: float = HOLD_MINUTES
    # 'HH:MM' van local time; None = no automatic turn-on.
    morning_on: str | None = MORNING_ON
    # Never shut off between these ('HH:MM'); both None = no window.
    quiet_start: str | None = None
    quiet_end: str | None = None
    # The van's zone; None = host local. Set per tick.
    tz: tzinfo | None = None


DEFAULT_SETTINGS = Settings()


@dataclass(frozen=True)
class State:
    """What the controller carries between ticks (and restarts, via kv)."""
    off_by_idle: bool = False
    off_ts: int | None = None
    below_since: int | None = None
    pending_mode: int | None = None
    pending_ts: int | None = None
    pending_reason: str | None = None
    last_write_ts: int = 0
    hold_until: int = 0
    # The mode as last seen, to tell a hand change from our own write.
    last_mode: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "State":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        except (ValueError, TypeError):
            return cls()


@dataclass(frozen=True)
class Readings:
    now: int
    load_w: float | None          # AC consumption (what the loads draw)
    mode: int | None              # vebus Mode
    inverter_state: int | None    # vebus State
    shore_connected: bool | None
    connected: bool               # the local Cerbo link, which writes go over
    turn_on_requested: bool = False


@dataclass
class Decision:
    state: State
    write_mode: int | None = None
    events: list[Event] = field(default_factory=list)


def in_quiet(settings: Settings, now: int) -> bool:
    if not settings.quiet_start or not settings.quiet_end:
        return False
    t = local(now, settings.tz).time()
    start, end = _t(settings.quiet_start), _t(settings.quiet_end)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end            # wraps midnight, e.g. 18:00-00:30


def morning_due(settings: Settings, off_ts: int | None, now: int) -> bool:
    """True once the first morning_on after the shut-off has passed."""
    if not settings.morning_on or off_ts is None:
        return False
    off = local(off_ts, settings.tz)
    at = datetime.combine(off.date(), _t(settings.morning_on), tzinfo=off.tzinfo)
    if at <= off:
        at += timedelta(days=1)
    return now >= int(at.timestamp())


def _hhmm(ts: int, tz: tzinfo | None) -> str:
    return local(ts, tz).strftime("%H:%M")


def decide(settings: Settings, st: State, r: Readings) -> Decision:
    d = Decision(state=st)
    now = r.now

    def emit(type_: str, sev: Severity, detail: dict) -> None:
        d.events.append(make(type_, sev, detail, ts=now))

    def write(mode: int, reason: str) -> None:
        d.write_mode = mode
        d.state = replace(d.state, pending_mode=mode, pending_ts=now,
                          pending_reason=reason, last_write_ts=now, below_since=None)

    # 1. A write in flight: confirmed by the read-back, or given up on.
    if st.pending_mode is not None:
        if r.mode == st.pending_mode:
            if st.pending_mode == MODE_CHARGER_ONLY:
                d.state = replace(st, off_by_idle=True, off_ts=now, pending_mode=None,
                                  pending_ts=None, pending_reason=None, last_mode=r.mode)
                detail = {"summary": f"Inverter set to charger-only: AC load under "
                                     f"{settings.threshold_w:.0f} W for {settings.minutes:.0f} min",
                          "threshold_w": settings.threshold_w, "minutes": settings.minutes}
                if settings.morning_on:
                    detail["morning_on"] = settings.morning_on
                emit("inverter_idle_off", Severity.INFO, detail)
            else:
                d.state = replace(st, off_by_idle=False, off_ts=None, pending_mode=None,
                                  pending_ts=None, pending_reason=None, last_mode=r.mode,
                                  hold_until=now + int(settings.hold_minutes * 60))
                emit("inverter_on", Severity.INFO,
                     {"summary": f"Inverter back on ({st.pending_reason})",
                      "reason": st.pending_reason})
            return d
        if now - (st.pending_ts or now) >= CONFIRM_SECONDS:
            d.state = replace(st, pending_mode=None, pending_ts=None, pending_reason=None)
            emit("inverter_write_failed", Severity.WARN,
                 {"summary": f"The inverter didn't change to mode {st.pending_mode} "
                             f"({st.pending_reason}) within {CONFIRM_SECONDS} s",
                  "mode": st.pending_mode, "reason": st.pending_reason, "seen_mode": r.mode})
        return d

    # 2. A mode change we didn't make: the owner's, always respected.
    if r.mode is not None and st.last_mode is not None and r.mode != st.last_mode:
        changes = {"last_mode": r.mode, "below_since": None}
        if st.off_by_idle:
            changes.update(off_by_idle=False, off_ts=None)
            emit("inverter_idle_cleared", Severity.INFO,
                 {"summary": "Inverter mode changed by hand; idle shut-off cleared",
                  "mode": r.mode})
        if r.mode == MODE_ON:
            changes["hold_until"] = now + int(settings.hold_minutes * 60)
        d.state = st = replace(st, **changes)
    elif r.mode is not None and st.last_mode is None:
        d.state = st = replace(st, last_mode=r.mode)

    can_write = r.connected and now - st.last_write_ts >= RETRY_SECONDS

    # 3. Back on: the owner's button (from charger-only, whoever set it), or
    #    the morning after our own shut-off. Runs even with the feature turned
    #    off, so disabling it can't strand the inverter in charger-only.
    if r.mode == MODE_CHARGER_ONLY and r.connected:
        if r.turn_on_requested:
            write(MODE_ON, "button")
            return d
        if st.off_by_idle and morning_due(settings, st.off_ts, now) and can_write:
            write(MODE_ON, f"morning {settings.morning_on}")
            return d

    # 4. Idle shut-off.
    eligible = (settings.enabled and r.connected and not st.off_by_idle
                and r.mode == MODE_ON and r.inverter_state == STATE_INVERTING
                and r.shore_connected is not True and now >= st.hold_until
                and not in_quiet(settings, now) and r.load_w is not None)
    if not eligible or r.load_w >= settings.threshold_w:
        if st.below_since is not None:
            d.state = replace(st, below_since=None)
        return d
    since = st.below_since if st.below_since is not None else now
    if since != st.below_since:
        d.state = replace(st, below_since=since)
    if now - since >= settings.minutes * 60 and can_write:
        write(MODE_CHARGER_ONLY, "idle")
    return d


def status(settings: Settings, st: State, now: int) -> dict:
    """The UI's view: what the controller is doing, in plain terms."""
    out = {"enabled": settings.enabled, "off_by_idle": st.off_by_idle,
           "off_ts": st.off_ts, "pending": st.pending_reason,
           "threshold_w": settings.threshold_w, "minutes": settings.minutes,
           "morning_on": settings.morning_on, "idle_s": None, "hold_until": None,
           "quiet": in_quiet(settings, now)}
    if st.below_since is not None:
        out["idle_s"] = max(0, now - st.below_since)
    if st.hold_until > now:
        out["hold_until"] = st.hold_until
    return out


def request_path(config_path: str | Path) -> Path:
    return Path(config_path).with_name(REQUEST_FILE)


def write_request(config_path: str | Path, now: int, via: str) -> None:
    """The UI's "Turn inverter on": a small file the collector picks up."""
    p = request_path(config_path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".inverter-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"ts": int(now), "via": via}, f)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_request(config_path: str | Path) -> int | None:
    """When the button was last pressed, or None."""
    try:
        return int(json.loads(request_path(config_path).read_text())["ts"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
