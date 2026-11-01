"""Where the van is and what time it is there.

Two things that were fixed to this rig's home and host:

* **Position.** Weather was fetched for the latitude/longitude in config.yaml
  (northern NJ), wherever the van actually was. A GX with a GPS receiver
  publishes ``gps/<inst>/Position/{Latitude,Longitude}`` and ``gps/<inst>/Fix``;
  with a fix, that wins. This rig has no receiver (no gps service at all,
  verified 2026-10-31), so it keeps using the configured position.
* **Time zone.** The nightly solar-path day, its 21:00 processing hour and the
  drive report's times were all in the *host's* local time. They should be in
  the van's. Resolution: ``system.timezone`` in config.yaml if set, else the
  GX's own setting (``settings/0/Settings/System/TimeZone``, which the owner
  changes on the GX while travelling), else the host's. The GX value is kept
  in kv so restarts and offline tools see it before the Cerbo answers.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

KV_TIMEZONE = "van.timezone"
KV_POSITION = "van.position"

_GPS = re.compile(r"/gps/(?P<inst>\d+)/(?P<field>Position/Latitude|Position/Longitude|Fix)$")
_TZ = re.compile(r"/settings/0/Settings/System/TimeZone$")

# A fix older than this is not "where the van is" any more; fall back.
GPS_STALE_SECONDS = 1800
# Likewise a fix from the owner's phone (cerberus.phonefix): the phone sends
# while the web app is open, so it goes quiet whenever the app is closed.
PHONE_STALE_SECONDS = 1800
# Stored position only changes when the van has moved this far (~1 km), so a
# parked van's GPS jitter does not rewrite kv on every message.
POSITION_STEP_DEG = 0.01


def local(ts: int, tz: tzinfo | None = None) -> datetime:
    """ts as a wall-clock datetime in tz; None means host local, as before."""
    return datetime.fromtimestamp(ts, tz)


def day_start(day: str, tz: tzinfo | None = None) -> int:
    """Epoch seconds of local midnight starting YYYY-MM-DD in tz.

    The day's end is the next date's day_start, not this plus 86400: across a
    DST change a local day is 23 or 25 hours long.
    """
    d = datetime.strptime(day, "%Y-%m-%d")
    if tz is not None:
        d = d.replace(tzinfo=tz)
    return int(d.timestamp())


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def zone(name: str | None) -> tzinfo | None:
    """A ZoneInfo for an IANA name, or None (host local) if unset or unknown."""
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown time zone %r — using host local time", name)
        return None


class VanLocation:
    """Folds the GX's GPS and time-zone topics into the current position and
    zone, persisting what is worth surviving a restart."""

    def __init__(self, kv=None):
        self.kv = kv
        self.lat: float | None = None
        self.lon: float | None = None
        self.fix: bool = False
        self.fix_ts: int = 0
        self.gx_timezone: str | None = None
        # Any gps topic at all: the service exists only with a receiver
        # fitted, fix or not (setup detection reports it).
        self.gps_seen: bool = False
        self._stored_pos: tuple[float, float] | None = None
        self._stored_source = "gps"
        # The owner's phone, when it is with the van: (lat, lon, ts).
        self.phone: tuple[float, float, int] | None = None
        if kv is not None:
            self.gx_timezone = kv.get(KV_TIMEZONE) or None
            raw = kv.get(KV_POSITION)
            if raw:
                try:
                    p = json.loads(raw)
                    self._stored_pos = (float(p["lat"]), float(p["lon"]))
                    self._stored_source = str(p.get("source") or "gps")
                except (ValueError, KeyError, TypeError):
                    pass

    def update(self, topic: str, value, now: int | None = None) -> bool:
        """Fold one topic in. True if it was a location topic."""
        now = int(time.time()) if now is None else now
        if _TZ.search(topic):
            name = None if value is None else str(value).strip() or None
            if name and name != self.gx_timezone:
                self.gx_timezone = name
                if self.kv is not None:
                    self.kv[KV_TIMEZONE] = name
            return True
        m = _GPS.search(topic)
        if not m:
            return False
        self.gps_seen = True
        field = m.group("field")
        if field == "Fix":
            self.fix = value is not None and int(value) == 1
        else:
            try:
                v = None if value is None else float(value)
            except (TypeError, ValueError):
                v = None
            if field.endswith("Latitude"):
                self.lat = v
            else:
                self.lon = v
        if self.fix and self.lat is not None and self.lon is not None:
            self.fix_ts = now
            self._remember(self.lat, self.lon, now)
        return True

    def phone_fix(self, lat: float, lon: float, ts: int) -> None:
        """A position from the owner's phone, already judged to be with the
        van (cerberus.phonefix). The GX's own GPS still wins when it has one."""
        self.phone = (lat, lon, ts)
        self._remember(lat, lon, ts, source="phone")

    def _remember(self, lat: float, lon: float, now: int, source: str = "gps") -> None:
        old = self._stored_pos
        if (old and source == self._stored_source
                and abs(old[0] - lat) < POSITION_STEP_DEG and abs(old[1] - lon) < POSITION_STEP_DEG):
            return
        self._stored_pos, self._stored_source = (lat, lon), source
        if self.kv is not None:
            self.kv[KV_POSITION] = json.dumps({"lat": lat, "lon": lon, "ts": now, "source": source})

    def position(self, fallback_lat: float, fallback_lon: float,
                 now: int | None = None) -> tuple[float, float, str]:
        """(lat, lon, source): a live GPS fix if there is one, else a recent
        fix from the owner's phone ("phone"), else the last fix on record
        ("gps_last" / "phone_last": the van was there most recently), else
        the configured home position.

        "Live" is judged by fix_ts, which only moves when a message arrives;
        a parked van's position does not change, but the periodic full
        republish (mqtt.full_republish_seconds, 300 s) resends it well inside
        GPS_STALE_SECONDS.
        """
        now = int(time.time()) if now is None else now
        if (self.fix and self.lat is not None and self.lon is not None
                and now - self.fix_ts <= GPS_STALE_SECONDS):
            return self.lat, self.lon, "gps"
        if self.phone is not None and now - self.phone[2] <= PHONE_STALE_SECONDS:
            return self.phone[0], self.phone[1], "phone"
        if self._stored_pos is not None:
            return self._stored_pos[0], self._stored_pos[1], f"{self._stored_source}_last"
        return fallback_lat, fallback_lon, "config"

    def timezone(self, configured: str | None = None) -> tzinfo | None:
        """Configured zone, else the GX's, else None (host local)."""
        return zone(configured) or zone(self.gx_timezone)
