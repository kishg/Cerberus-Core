"""The GX link, in words an owner can act on (#66).

"Cerbo offline" alone left the owner guessing: since when, whether Victron's cloud is filling in,
and whether the GX is gone entirely or only unreachable on this network. The collector keeps the
few facts that answer those, and puts this view in the live snapshot (the box UI's status line),
the HA ``cerbo_online`` sensor's attributes, and the health report.

* **lan**: "ok" while the GX's heartbeat arrives over the local network, else "down".
* **lan_last**: when it last did (None: not since the collector started).
* **cause** (only while down, when the box can tell): "no_answer" (nothing answers at the GX's
  address: off, or not on this network), "refused" (the address answers but MQTT is refused:
  the broker is off, or the address belongs to something else), or "silent" (connected to the
  GX's broker, but the GX sends nothing).
* **fallback**: Victron's cloud (VRM MQTT), only used while the local link is down:
  "off" (not turned on), "no_token" (on, but no VRM token), "waiting" (the local link has been down
  less than cloud_after_seconds), "connecting", "no_data" (connected, but nothing real has come
  from the GX: the relay's retained Serial doesn't count), or "delivering".
* **fallback_last**: when the cloud last delivered something real.
* **via** (only while up): which way the box reaches the GX: "gx_port" (the GX cabled to the box's
  second Ethernet port, cerberus.gxport) or "lan" (through the van's network). Box UI and HA only:
  the health report keeps its own allowlist.
"""

from __future__ import annotations

import errno
import socket
import time

FRESH_S = 60                 # the same as the heartbeat's staleness limit
PROBE_EVERY_S = 60
PROBE_TIMEOUT_S = 1.5
# What the relay sends with no GX behind it: retained topics and the keepalive's echo.
NOT_DATA = ("/Serial", "/keepalive")

NO_ANSWER, REFUSED, SILENT = "no_answer", "refused", "silent"


def probe(ip: str | None, port: int, timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """Why the GX's broker can't be reached: NO_ANSWER, REFUSED, or None (it accepts, or unknown)."""
    if not ip:
        return None
    try:
        with socket.create_connection((ip, port), timeout):
            return None
    except ConnectionRefusedError:
        return REFUSED
    except (TimeoutError, socket.timeout):
        return NO_ANSWER
    except OSError as e:
        if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
            return NO_ANSWER
        return None


class LinkWatch:
    """Fed by the collector's MQTT callbacks (any thread) and its main loop."""

    def __init__(self, fallback_enabled: bool, has_token: bool, after_s: int):
        self.fallback_enabled = fallback_enabled
        self.has_token = has_token
        self.after_s = after_s
        self.lan_last: int | None = None
        self.cloud_started: int | None = None
        self.cloud_connected = False
        self.cloud_last: int | None = None
        self.cause: str | None = None
        self._probed = 0.0

    # -- callbacks --------------------------------------------------------------

    def lan_heartbeat(self, now: float | None = None) -> None:
        self.lan_last = int(time.time() if now is None else now)
        self.cause = None

    def cloud_message(self, topic: str, retained: bool, now: float | None = None) -> None:
        """Any message from the cloud relay; only fresh ones from the GX itself count."""
        if retained or topic.endswith(NOT_DATA):
            return
        self.cloud_last = int(time.time() if now is None else now)

    def cloud_up(self, now: float | None = None) -> None:
        self.cloud_started = int(time.time() if now is None else now)
        self.cloud_connected = False
        self.cloud_last = None

    def cloud_down(self) -> None:
        self.cloud_started = None
        self.cloud_connected = False

    # -- main loop ----------------------------------------------------------------

    def lan_ok(self, now: float) -> bool:
        return self.lan_last is not None and now - self.lan_last < FRESH_S

    def maybe_probe(self, now: float, ip: str | None, port: int, connected: bool = False, probe_fn=probe) -> None:
        """While the local link is down, find out why (at most once a minute; it blocks briefly)."""
        if self.lan_ok(now):
            self.cause = None
            return
        if connected:                       # the broker took us, but no heartbeat comes
            self.cause = SILENT
            return
        if now - self._probed < PROBE_EVERY_S:
            return
        self._probed = now
        self.cause = probe_fn(ip, port)

    def fallback_state(self, now: float, local_down_since: float | None = None) -> str:
        if not self.fallback_enabled:
            return "off"
        if not self.has_token:
            return "no_token"
        if self.cloud_started is None:
            return "waiting"
        if self.cloud_last is not None and now - self.cloud_last < FRESH_S:
            return "delivering"
        return "no_data" if self.cloud_connected else "connecting"

    def view(self, now: float, local_down_since: float | None = None, via: str | None = None) -> dict:
        ok = self.lan_ok(now)
        return {
            "lan": "ok" if ok else "down",
            "lan_last": self.lan_last,
            "cause": None if ok else self.cause,
            # The fallback only runs while the local link is down; with it up, there's nothing to say.
            "fallback": None if ok else self.fallback_state(now, local_down_since),
            "fallback_last": None if ok else self.cloud_last,
            "via": via if ok else None,
        }
