"""Full-rate voltage and current around alarms and heavy loads.

Why this exists
---------------
The collector stores one value per signal every 30 s: the latest, not the
lowest. That hid where the MultiPlus's LowBattery alarm trips: on 2026-10-21
heater runs reached 12.42 V and 12.48 V with no alarm, and on 10-24 and 10-25
runs reached 12.44 V and 12.45 V with one. The GX sends the inverter's terminal
voltage on every change and the owner watches the dip live on its screen;
Cerberus receives every message and threw them away.

So: keep the last ``pre_seconds`` of raw Dc/0 Voltage and Current messages
(packs, inverter, MPPT) in memory, and store them at full rate in a window
around anything worth seeing:

* **an alarm** raised or cleared on any Victron device: from pre_seconds before
  to post_seconds after;
* **a heavy AC load**: while the load is more than on_delta_w above its
  baseline (the cerberus.heater rule, which is when cable drop is measurable,
  and comes before most alarms), until it falls under off_delta_w above that
  baseline, plus post_seconds.

Rows go to ``fast_samples``; ``fast_windows`` says when and why. Each signal is
limited to one reading per min_interval_seconds and rows older than
retention_days are deleted, so a long load can't fill the disk.

The GX publishes on change, not on a timer (measured 2026-11-01, parked: inverter
voltage 0.2 messages/s, MPPT voltage 0.5/s, a pack's current only when it moves).
So a signal with no row in a window held its last value: carry it forward when
reading these rows.

The MQTT callbacks arrive on the client's thread and tick() on the main loop,
hence the lock.
"""

from __future__ import annotations

import logging
import re
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .db import Db

log = logging.getLogger(__name__)

_DC = re.compile(r"/(?P<svc>battery|vebus|solarcharger)/(?P<inst>\d+)/Dc/0/(?P<f>Voltage|Current)$")

FLUSH_ROWS = 200
FLUSH_SECONDS = 2.0
PRUNE_EVERY_S = 86400
BASELINE_WINDOW_S = 600
DAY = 86400


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    pre_seconds: float = 60.0
    post_seconds: float = 60.0
    min_interval_seconds: float = 0.25
    retention_days: float = 90.0
    # The heater rule (cerberus.heater.Settings), passed in from config.
    on_delta_w: float = 800.0
    off_delta_w: float = 400.0
    baseline_max_w: float = 800.0
    default_baseline_w: float = 150.0


def series_name(topic: str) -> str | None:
    """"battery/1/Voltage" and the like for a Dc/0 topic, else None."""
    m = _DC.search(topic)
    return f"{m['svc']}/{m['inst']}/{m['f']}" if m else None


class FastCapture:
    def __init__(self, db: Db, settings: Settings = Settings(),
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.s, self.clock = db, settings, clock
        self._lock = threading.Lock()
        self._ring: deque[tuple[float, str, float]] = deque()
        self._last: dict[str, float] = {}
        self._pending: list[tuple[float, str, float]] = []
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._until = 0.0               # active until this time, once load is off
        self._heavy = False
        self._heavy_base = 0.0
        self._window: int | None = None
        self._reasons: list[str] = []
        self._base: deque[tuple[float, float]] = deque()

    # ------------------------------------------------------------- inputs

    def on_dc(self, topic: str, value) -> None:
        if not self.s.enabled or not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        name = series_name(topic)
        if name is None:
            return
        t = self.clock()
        with self._lock:
            if t - self._last.get(name, 0.0) < self.s.min_interval_seconds:
                return
            self._last[name] = t
            row = (t, name, float(value))
            self._ring.append(row)
            cutoff = t - self.s.pre_seconds
            while self._ring and self._ring[0][0] < cutoff:
                self._ring.popleft()
            if self._active(t):
                self._pending.append(row)
                if len(self._pending) >= FLUSH_ROWS:
                    self._flush(t)

    def on_alarm(self, alarm_id: str) -> None:
        """An alarm was raised or cleared."""
        if not self.s.enabled:
            return
        t = self.clock()
        with self._lock:
            self._open(t, f"alarm:{alarm_id}")
            self._until = max(self._until, t + self.s.post_seconds)

    def on_load(self, watts: float | None) -> None:
        """Total AC load, as often as it arrives. None: the inverter is off."""
        if not self.s.enabled:
            return
        t = self.clock()
        with self._lock:
            if self._heavy:
                if watts is None or watts < self._heavy_base + self.s.off_delta_w:
                    self._heavy = False
                    self._until = max(self._until, t + self.s.post_seconds)
                return
            if watts is None:
                return
            base = self._baseline(t)
            if watts > base + self.s.on_delta_w:
                self._heavy, self._heavy_base = True, base
                self._open(t, "load")
            elif watts <= self.s.baseline_max_w:
                self._base.append((t, float(watts)))

    def tick(self) -> None:
        """Flush what's pending, close a finished window, prune old rows."""
        if not self.s.enabled:
            return
        t = self.clock()
        with self._lock:
            if self._pending and t - self._last_flush >= FLUSH_SECONDS:
                self._flush(t)
            if self._window is not None and not self._active(t):
                self._flush(t)
                self._write_window(self._until)
                log.info("fast capture: window closed (%s)", ",".join(self._reasons))
                self._window, self._reasons = None, []
            if t - self._last_prune >= PRUNE_EVERY_S:
                self._last_prune = t
                self.db.prune_fast_samples(t - self.s.retention_days * DAY)

    # ------------------------------------------------------------ internals

    def _active(self, t: float) -> bool:
        return self._heavy or t < self._until

    def _baseline(self, t: float) -> float:
        cutoff = t - BASELINE_WINDOW_S
        while self._base and self._base[0][0] < cutoff:
            self._base.popleft()
        if not self._base:
            return self.s.default_baseline_w
        return statistics.median(w for _, w in self._base)

    def _open(self, t: float, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)
        if self._window is None:
            # A new window starts with the last pre_seconds. Inside a window
            # everything is already pending or stored.
            start = self._ring[0][0] if self._ring else t
            self._pending.extend(self._ring)
            self._window = self.db.open_fast_window(start, ",".join(self._reasons))
            log.info("fast capture: window opened (%s)", reason)
        else:
            self._write_window(None)

    def _write_window(self, end: float | None) -> None:
        if self._window is not None:
            self.db.update_fast_window(self._window, end, ",".join(self._reasons))

    def _flush(self, t: float) -> None:
        rows, self._pending = self._pending, []
        self._last_flush = t
        if rows:
            self.db.insert_fast_samples(rows)
