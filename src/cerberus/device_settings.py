"""Victron settings history: what the Cerbo, MPPT, MultiPlus and BMSes are set to.

The GX publishes its own settings tree (DVCC, shared voltage sense, alarm
levels, relays, ESS) and a few device settings and limits (the MPPT's current
limit, the MultiPlus's AC input limit, each BMS's charge limits, firmware
versions) on the same MQTT feed Cerberus already reads. Nothing is sent to get
them. Each value is stored when first seen and again whenever it changes, so
"what changed, and when" has an answer. Recorded 2026-11-10.

What is NOT here, because the GX never publishes it: the MPPT's absorption and
float voltages, and the MultiPlus's charger voltages and low-battery
shut-down/restart. Those stay in VictronConnect / VE.Configure exports.

A null value (a device switched off: with the inverter off the whole VE.Bus
tree reads null) is "unknown", never a change. A path that keeps changing is
muted for the rest of the run, so one chatty setting cannot flood the table.

Optional ``victron.expect`` maps paths to the values the owner set; a value
that differs raises ``victron_settings_unexpected`` (a finding) and clears
when every expected path matches again.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from .events import Event, Severity

log = logging.getLogger("cerberus.device_settings")

# Paths after "N/<portal>/". Settings proper, plus device values that behave
# like settings (set by the owner or the BMS, not measurements).
_INCLUDE = re.compile(
    r"^(settings/0/Settings/"
    r"|solarcharger/\d+/(Settings/|Mode$|FirmwareVersion$)"
    r"|vebus/\d+/(Devices/\d+/(Settings/|FirmwareVersion$)|FirmwareVersion$|Ac/In/\d+/CurrentLimit$)"
    r"|battery/\d+/(Info/(MaxChargeVoltage|MaxChargeCurrent|MaxDischargeCurrent|BatteryLowVoltage)$"
    r"|FirmwareVersion$)"
    r"|platform/0/Firmware/Installed/Version$)"
)
# Counters and VRM-written schedules that live in the settings tree but
# change by themselves.
_EXCLUDE = re.compile(
    r"(Accumulated|DischargedTime|BatteryLife/(SocLimit|State|Flags)"
    r"|/DynamicEss/|/TestRun/SkipRuntime|/Vrmlogger/)"
)
_PORTAL = re.compile(r"^N/[^/]+/")

# A path that changes more often than this within an hour is muted for the run.
MUTE_CHANGES_PER_HOUR = 10

# Plain names for the paths most likely to be in victron.expect.
LABELS = {
    "settings/0/Settings/Services/Bol": "DVCC",
    "settings/0/Settings/SystemSetup/SharedVoltageSense": "Shared voltage sense (DVCC)",
    "settings/0/Settings/SystemSetup/SharedTemperatureSense": "Shared temperature sense (DVCC)",
    "settings/0/Settings/SystemSetup/MaxChargeCurrent": "DVCC max charge current",
    "settings/0/Settings/SystemSetup/MaxChargeVoltage": "DVCC max charge voltage",
    "settings/0/Settings/Alarm/Vebus/LowBattery": "Inverter low-battery alarm",
}


def path_of(topic: str) -> str | None:
    """The settings path of an MQTT topic, or None if it isn't one we keep."""
    path = _PORTAL.sub("", topic, count=1)
    if not _INCLUDE.match(path) or _EXCLUDE.search(path):
        return None
    return path


def label(path: str) -> str:
    if path in LABELS:
        return LABELS[path]
    m = re.match(r"^(solarcharger|vebus|battery)/(\d+)/(.*)$", path)
    if m:
        dev = {"solarcharger": "MPPT", "vebus": "Inverter/charger",
               "battery": "Battery"}[m.group(1)]
        return f"{dev} {m.group(2)} {m.group(3)}"
    return path.removeprefix("settings/0/Settings/")


def same(a, b) -> bool:
    """Equal, with floats compared loosely: the GX sends 14.399999618530273.
    A bool never equals a number (Python's 1 == True would hide a change)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-3
    return a == b


class SettingsRecorder:
    """Called for every MQTT message (from paho's thread, and the VRM cloud
    client's while that runs); writes only first sightings and changes."""

    def __init__(self, db, expect: dict | None = None) -> None:
        self.db = db
        self.expect = dict(expect or {})
        self._lock = threading.Lock()
        self._last: dict[str, object] = db.latest_device_settings()
        self._changes: dict[str, list[float]] = {}
        self._muted: set[str] = set()

    def on_message(self, topic: str, value, now: int | None = None) -> None:
        path = path_of(topic)
        if path is None or value is None or path in self._muted:
            return
        with self._lock:
            known = path in self._last
            old = self._last.get(path)
            if known and same(old, value):
                return
            ts = int(time.time()) if now is None else now
            if known and self._too_chatty(path, ts):
                return
            if not self.db.insert_device_setting(ts, path, value):
                return
            self._last[path] = value
            if known:
                log.info("setting changed: %s %r -> %r", path, old, value)
                self.db.insert_event(Event(ts, "victron_setting_changed", Severity.INFO,
                                           {"path": path, "old": old, "new": value}))
            if path in self.expect:
                self._check_expect(ts)

    def _too_chatty(self, path: str, ts: float) -> bool:
        recent = [t for t in self._changes.get(path, []) if ts - t < 3600] + [ts]
        self._changes[path] = recent
        if len(recent) > MUTE_CHANGES_PER_HOUR:
            self._muted.add(path)
            log.warning("setting %s changed %d times in an hour; no longer recorded "
                        "this run (add it to the exclusions if it is a counter)",
                        path, len(recent))
            return True
        return False

    def _check_expect(self, ts: int) -> None:
        off = {p: [want, self._last.get(p)] for p, want in self.expect.items()
               if p in self._last and not same(self._last[p], want)}
        last = self.db.conn.execute(
            "SELECT type, detail FROM events WHERE type IN "
            "('victron_settings_unexpected', 'victron_settings_unexpected_cleared') "
            "ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
        raised = last is not None and last[0] == "victron_settings_unexpected"
        raised_off = (json.loads(last[1]).get("detail", {}).get("off")
                      if raised else None)
        if off and off != raised_off:
            # A new raise whenever the set of wrong values changes, so the
            # finding shows the current ones.
            log.warning("settings differ from victron.expect: %s", off)
            self.db.insert_event(Event(ts, "victron_settings_unexpected", Severity.WARN,
                                       {"off": off}))
        elif not off and raised:
            self.db.insert_event(Event(ts, "victron_settings_unexpected_cleared",
                                       Severity.INFO, {}))
