from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import MqttConfig

log = logging.getLogger(__name__)

RECONNECT_GRACE_SECONDS = 2.0
ROTATE_AFTER_ATTEMPTS = 3              # consecutive failures before trying next host
STICKY_CONNECT_SECONDS = 60.0          # connected this long resets the attempt counter
CACHED_IP_TTL_SECONDS = 300.0          # past this, a cached IP is assumed wrong, not stale
WATCHDOG_INTERVAL_SECONDS = 30.0       # how often to re-resolve while disconnected
FORCE_RECONNECT_COOLDOWN_SECONDS = 90.0  # floor between caller-forced reconnects
PORT_PROBE_TIMEOUT_SECONDS = 2.0       # TCP reachability probe before rotating
SUPPRESS_REPUBLISH_PAYLOAD = b'{"keepalive-options": ["suppress-republish"]}'
MAX_ROTATION_SUPPRESSIONS = 10        # cap on consecutive "reachable, don't rotate"


class CerboMqtt:
    """Read-only Victron Cerbo GX MQTT client, plus outbound publishing for
    Cerberus's own state/event/discovery topics.

    - Authenticates with portal_id + Remote Console password.
    - Subscribes to system/0/Batteries (primary) + heartbeat (+ optional
      per-instance Dc topics).
    - Publishes to R/<portal>/keepalive on connect and every
      keepalive_publish_seconds; without this the GX stops publishing. Only
      the connect keepalive and one every full_republish_seconds are empty
      (full republish); the rest carry suppress-republish.
    - LWT: ``<publish_namespace>/availability`` retained → ``offline`` on broker-
      detected disconnect. On successful connect publishes ``online``.
    - Never publishes ``W/`` topics into the Victron tree.
    """

    # A class default too, so a dispatcher built without __init__ has it.
    on_setting = None

    def __init__(
        self,
        cfg: MqttConfig,
        on_batteries: Callable[[list[dict]], None],
        on_heartbeat: Callable[[int], None],
        on_ready: Callable[[], None] | None = None,
        on_alarm: Callable[[str, int | None], None] | None = None,
        on_ac: Callable[[str, object], None] | None = None,
        on_solar: Callable[[str, object], None] | None = None,
        on_battery_health: Callable[[str, object], None] | None = None,
        on_temperature: Callable[[str, object], None] | None = None,
        on_wifi: Callable[[str, object], None] | None = None,
        on_dc: Callable[[str, object], None] | None = None,
        on_location: Callable[[str, object], None] | None = None,
        on_setting: Callable[[str, object], None] | None = None,
        tls_context: ssl.SSLContext | None = None,
        own_topics: bool = True,
    ) -> None:
        self.cfg = cfg
        self.on_batteries = on_batteries
        self.on_heartbeat = on_heartbeat
        self.on_ready = on_ready
        self.on_alarm = on_alarm
        self.on_ac = on_ac
        self.on_solar = on_solar
        self.on_battery_health = on_battery_health
        self.on_temperature = on_temperature
        self.on_wifi = on_wifi
        self.on_dc = on_dc
        self.on_location = on_location
        self.on_setting = on_setting
        self.availability_topic = f"{cfg.publish_namespace}/availability"
        # False for the VRM cloud relay: its ACL only admits the installation's
        # own N/R/W tree, so the availability LWT and "online" marker (which
        # live in Cerberus's namespace) must not be sent there.
        self.own_topics = own_topics
        # With TLS paho must be handed the hostname: the certificate is checked
        # against whatever it connects to, and an IP never matches it.
        self._tls = tls_context is not None
        self._stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self._last_full_republish = 0.0
        # Failover state. hosts is ordered; current_idx is which one we're
        # actively trying. attempts_since_last_success counts both DNS and
        # connect failures on the current host. _connected_since stamps the
        # last successful on_connect; surviving STICKY_CONNECT_SECONDS resets
        # the attempt counter (treats it as a "real" session).
        self._hosts: tuple[str, ...] = cfg.hosts or (cfg.host,)
        self._current_idx: int = 0
        self._cached_ip: dict[str, tuple[str, float]] = {}  # host -> (ip, resolved_at)
        self._attempts_since_last_success: int = 0
        self._rotation_suppressions: int = 0
        self._connected_since: float | None = None
        # _connected is the watchdog's view of the link. paho fires neither
        # on_connect nor on_disconnect while a *connect attempt* is failing, so
        # this flag plus the watchdog thread are the only way to notice that
        # we've been trying to reach an unreachable address for hours.
        self._connected: bool = False
        self._target_ip: str | None = None    # address paho was last pointed at
        self._retarget_lock = threading.RLock()
        self._watchdog_thread: threading.Thread | None = None
        # force_reconnect state. _forcing suppresses the on_disconnect
        # reconnect path so the teardown doesn't race its own recovery.
        self._last_forced_reconnect: float = 0.0
        self._forcing: bool = False

        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"cerberus-{'' if own_topics else 'cloud-'}{int(time.time())}",
        )
        self.client.username_pw_set(cfg.username, cfg.password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        if tls_context is not None:
            self.client.tls_set_context(tls_context)
        # LWT must be set before connect.
        if own_topics:
            self.client.will_set(self.availability_topic, payload=b"offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @property
    def current_host(self) -> str:
        return self._hosts[self._current_idx]

    @property
    def is_connected(self) -> bool:
        """Whether paho reports an established session. Note this can be True
        while the link is actually dead — that is precisely the condition
        force_reconnect exists to break out of."""
        return self._connected

    def force_reconnect(self, reason: str) -> bool:
        """Tear the session down and rebuild it. Returns True if issued.

        Motivated by the 2026-08-08 Starlink gaps: the TCP connection goes
        half-open, paho keeps reporting connected, and the qos-0 keepalive
        publishes vanish silently. The GX stops publishing 60s after the last
        keepalive it actually received, and nothing recovers until paho's own
        60s keepalive eventually fails the link — minutes of data lost each
        time. A caller that can see data has stopped needs a way to say "this
        connection is a lie, rebuild it".

        Full teardown (loop_stop + disconnect + fresh connect_async +
        loop_start) rather than paho's reconnect(), because an explicit
        disconnect stops paho's auto-reconnect thread and connect_async alone
        would not bring it back. Must not be called from a paho callback
        thread — loop_stop() joins that thread and would deadlock.
        """
        if self._stop.is_set():
            return False
        now = time.monotonic()
        since = now - self._last_forced_reconnect
        if self._last_forced_reconnect and since < FORCE_RECONNECT_COOLDOWN_SECONDS:
            log.debug("force_reconnect(%s) suppressed; %.0fs since last", reason, since)
            return False
        self._last_forced_reconnect = now
        self._connected = False
        self._forcing = True
        log.warning("forcing MQTT reconnect: %s", reason)
        try:
            try:
                self.client.loop_stop()
            except Exception:
                log.exception("loop_stop during force_reconnect failed")
            try:
                self.client.disconnect()
            except Exception:
                pass
            ip = self._resolve()
            self._retarget(ip)
            self.client.loop_start()
            log.info("forced reconnect issued -> %s (host=%s)", ip, self.current_host)
            return True
        except Exception:
            log.exception("force_reconnect failed; watchdog will retry")
            return False
        finally:
            self._forcing = False

    def _rotate_host(self) -> None:
        """Advance to the next host in the failover list. No-op if only one
        host is configured."""
        if len(self._hosts) <= 1:
            return
        prev = self.current_host
        self._current_idx = (self._current_idx + 1) % len(self._hosts)
        self._attempts_since_last_success = 0
        self._rotation_suppressions = 0
        log.warning("rotating MQTT host: %s -> %s", prev, self.current_host)

    def _port_open(self) -> bool:
        """Whether the address paho is currently aimed at accepts TCP on our port.

        This is the discriminator rotation was missing. "Not connected" conflates
        two unrelated situations:

          * the host is wrong or unreachable  -> rotating is the correct fix
          * the host is right and answering, but its broker has stopped serving
            -> rotating achieves nothing and costs a recovery window

        Only a connect attempt to the port tells them apart. Deliberately probes
        ``_target_ip`` rather than re-resolving: _resolve() rotates as a side
        effect on DNS failure, so calling it from here could move the host we are
        trying to make a decision about.
        """
        ip = self._target_ip
        if ip is None:
            return False
        try:
            with socket.create_connection((ip, self.cfg.port),
                                          PORT_PROBE_TIMEOUT_SECONDS):
                return True
        except OSError:
            return False

    def _note_failed_attempt(self, probe: bool = False) -> None:
        """Count a failure and rotate once ROTATE_AFTER_ATTEMPTS accumulate.

        ``probe`` enables the TCP reachability check before rotating. It is off
        by default because the paho callback threads call this too, and a
        blocking connect with a 2s timeout would stall the network loop. The
        watchdog is the only caller that needs it — and the only one that
        repeats, which is what made the 2026-09-09 incident expensive.
        """
        self._attempts_since_last_success += 1
        if (self._attempts_since_last_success < ROTATE_AFTER_ATTEMPTS
                or len(self._hosts) <= 1):
            return
        # Suppression is capped. A broker that is listening but permanently
        # broken should still eventually cost us a rotation to try elsewhere —
        # otherwise this trades one stuck state for another.
        if (probe
                and self._rotation_suppressions < MAX_ROTATION_SUPPRESSIONS
                and self._port_open()):
            self._rotation_suppressions += 1
            self._attempts_since_last_success = 0
            log.warning(
                "not rotating: %s accepts TCP on :%d but the feed is down "
                "(%d/%d) — broker-side stall, not a wrong host",
                self.current_host, self.cfg.port,
                self._rotation_suppressions, MAX_ROTATION_SUPPRESSIONS)
            return
        self._rotate_host()

    def _resolve(self, _tried: int = 0) -> str:
        """Resolve the current host to an IP, with per-host caching for graceful
        DNS failures. On 2026-06-08 the Cerbo's `venus.local` mDNS entry went
        stale overnight; paho's auto-reconnect machinery uses the IP cached at
        construct time and never re-resolves, so a stale binding becomes
        permanent until restart. We re-resolve before every connect attempt
        (start + every on_disconnect) so the next reconnect picks up a new IP
        if mDNS/DNS came back with one.

        The cached IP is a bridge over a *momentary* DNS hiccup, so it expires
        after CACHED_IP_TTL_SECONDS. Past that the likely explanation is not
        "mDNS is being slow" but "we changed networks" (home WiFi -> the van's
        Starlink), and then the cached address is not stale-but-right, it is
        simply wrong. Handing paho a wrong IP is worse than handing it the
        hostname: paho retries a literal address forever without re-resolving,
        whereas it calls getaddrinfo itself on every reconnect when given a
        name, which self-heals the moment DNS comes back.

        If DNS fails AND we have no usable cached IP AND there are other hosts
        configured, we rotate immediately — no point spinning on a name that
        won't resolve.
        """
        host = self.current_host
        if self._tls:
            return host
        try:
            infos = socket.getaddrinfo(host, self.cfg.port, type=socket.SOCK_STREAM)
            # Prefer IPv4 over IPv6, and skip link-local addresses (fe80::,
            # 169.254.x.x) — paho can't connect to those without a scope id.
            # On 2026-06-14 mDNS resolved venus.local to an IPv6 link-local
            # first; without this filter we'd hang forever trying to TCP it.
            def usable(info):
                family, _, _, _, sockaddr = info
                addr = sockaddr[0]
                if family == socket.AF_INET6 and addr.startswith("fe80"):
                    return False
                if family == socket.AF_INET and addr.startswith("169.254."):
                    return False
                return True
            v4 = [i for i in infos if usable(i) and i[0] == socket.AF_INET]
            v6 = [i for i in infos if usable(i) and i[0] == socket.AF_INET6]
            chosen = (v4 + v6)
            if not chosen:
                raise socket.gaierror(0, "no usable (non-link-local) addresses")
            ip = chosen[0][4][0]
            prev = self._cached_ip.get(host)
            if prev is None or prev[0] != ip:
                log.info("resolved %s -> %s", host, ip)
            self._cached_ip[host] = (ip, time.monotonic())
            return ip
        except socket.gaierror as e:
            cached = self._cached_ip.get(host)
            if cached is not None:
                cached_ip, resolved_at = cached
                age = time.monotonic() - resolved_at
                if age <= CACHED_IP_TTL_SECONDS:
                    log.warning("DNS lookup of %s failed (%s); reusing cached %s (age %.0fs)",
                                host, e, cached_ip, age)
                    return cached_ip
                log.warning(
                    "DNS lookup of %s failed (%s); discarding cached %s "
                    "(age %.0fs > %.0fs TTL — assuming the network changed)",
                    host, e, cached_ip, age, CACHED_IP_TTL_SECONDS)
                del self._cached_ip[host]
            log.error("DNS lookup of %s failed (%s) and no usable cache", host, e)
            # Try the next host, but only go around the ring once — every host
            # failing DNS (no network at all) would otherwise recurse forever.
            if len(self._hosts) > 1 and _tried < len(self._hosts) - 1:
                self._rotate_host()
                return self._resolve(_tried + 1)
            return host  # give paho the name; it re-resolves on each retry

    def start(self) -> None:
        if len(self._hosts) > 1:
            log.info("failover hosts (in order): %s", list(self._hosts))
        ip = self._resolve()
        log.info("connecting to %s:%s as %s (host=%s)",
                 ip, self.cfg.port, self.cfg.username, self.current_host)
        self._retarget(ip)
        self.client.loop_start()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="cerberus-keepalive", daemon=True
        )
        self._keepalive_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="cerberus-resolve-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _retarget(self, ip: str) -> None:
        """Point paho at ``ip`` for its next connect attempt.

        connect_async only updates the target; an in-flight attempt keeps
        running until it times out, after which paho's reconnect loop picks up
        the new address. That's why this is safe to call while the network
        loop is running.
        """
        with self._retarget_lock:
            self._target_ip = ip
            self.client.connect_async(ip, self.cfg.port, keepalive=60)

    def _watchdog_loop(self) -> None:
        """Re-resolve and re-target for as long as we are disconnected.

        paho retries whatever address it was last handed and never re-resolves
        it, and it fires no callback while a connect attempt is merely failing
        — so a wrong address is invisible and permanent. On 2026-08-07 that
        pinned the collector to the home LAN's IP after the van moved onto
        Starlink: hours in SYN_SENT, no log line, HA still rendering retained
        values so the rig looked healthy. Nothing else in this class can
        observe that state, which is why this loop exists.
        """
        while not self._stop.wait(WATCHDOG_INTERVAL_SECONDS):
            try:
                self._watchdog_tick()
            except Exception:
                log.exception("watchdog iteration failed")

    def _watchdog_tick(self) -> bool:
        """One watchdog iteration. Returns True if it re-targeted paho."""
        if self._connected:
            return False
        self._note_failed_attempt(probe=True)
        ip = self._resolve()
        if ip == self._target_ip:
            log.debug("watchdog: disconnected, target %s unchanged", ip)
            return False
        log.warning("watchdog: disconnected, re-targeting %s -> %s", self._target_ip, ip)
        self._retarget(ip)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._connected = False
        try:
            # courtesy "offline" before clean disconnect (so we don't depend on LWT)
            if self.own_topics:
                self.client.publish(self.availability_topic, b"offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def publish(self, topic: str, payload: str | bytes, retain: bool = False, qos: int = 0) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.client.publish(topic, payload=payload, qos=qos, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("connect failed (host=%s): %s", self.current_host, reason_code)
            self._connected = False
            self._note_failed_attempt()
            return
        self._connected = True
        self._connected_since = time.time()
        self._attempts_since_last_success = 0
        self._rotation_suppressions = 0
        portal = self.cfg.portal_id
        topics = [
            (f"N/{portal}/{self.cfg.primary_topic}", 0),
            (f"N/{portal}/heartbeat", 0),
            (f"N/{portal}/battery/+/Dc/0/#", 0),
            # Soh and Capacity are NOT in the system/0/Batteries array — that
            # payload stops at soc/voltage/current/power/temperature/timetogo.
            # They only exist per-instance, so they need their own subscription
            # or they are simply never seen (2026-09-19).
            # Temperature probes. Instance 24 reaches 67 C under charge current
            # and marks a hot joint nobody has identified; until now it existed
            # only on VRM at 15-minute resolution and in no local history at all.
            (f"N/{portal}/temperature/+/Temperature", 0),
            (f"N/{portal}/temperature/+/RawValue", 0),
            (f"N/{portal}/battery/+/Soh", 0),
            (f"N/{portal}/battery/+/Capacity", 0),
            (f"N/{portal}/+/+/Alarms/#", 0),
            # AC side (2026-08-08). Without these an AC load was only ever
            # visible as its DC reflection through the inverter, so "how much
            # is the kettle drawing" had no answer. system/0 gives the whole-
            # rig aggregates; vebus gives the inverter's own in/out split.
            (f"N/{portal}/system/0/Ac/#", 0),
            (f"N/{portal}/vebus/+/Ac/#", 0),
            (f"N/{portal}/vebus/+/State", 0),
            (f"N/{portal}/vebus/+/Mode", 0),
            # Inverter DC terminals (2026-10-19). The MultiPlus raised
            # LowBattery under a 1.5 kW water heater with the packs at 13.0 V
            # and 87% SoC; only its own terminal voltage can say whether that
            # is cable drop or just a high alarm setpoint.
            (f"N/{portal}/vebus/+/Dc/0/#", 0),
            # Solar (2026-08-09). Charge-source history: without it, "why was
            # yield low today" could only be answered from a live reading.
            (f"N/{portal}/solarcharger/+/#", 0),
            (f"N/{portal}/system/0/Dc/Pv/Power", 0),
            # The Cerbo's own WiFi link (2026-10-08). Its feed drops in
            # clusters while the Mac's internet stays up and VRM stays
            # complete, i.e. the Cerbo falls off the LAN. VRM does not carry
            # the signal; only this local tree does.
            (f"N/{portal}/platform/0/Network/Wifi/#", 0),
            # Where the van is and its local time (cerberus.location). The gps
            # service exists only with a receiver fitted; the time zone is a
            # GX setting the owner changes while travelling.
            (f"N/{portal}/gps/+/Position/#", 0),
            (f"N/{portal}/gps/+/Fix", 0),
            (f"N/{portal}/settings/0/Settings/System/TimeZone", 0),
            # Settings history (cerberus.device_settings, 2026-11-10): the
            # GX's own settings tree and the device settings and limits it
            # publishes. solarcharger/+/# and vebus/+/Ac/# are above already.
            (f"N/{portal}/settings/0/Settings/#", 0),
            (f"N/{portal}/vebus/+/Devices/+/Settings/#", 0),
            (f"N/{portal}/vebus/+/Devices/+/FirmwareVersion", 0),
            (f"N/{portal}/vebus/+/FirmwareVersion", 0),
            (f"N/{portal}/battery/+/Info/#", 0),
            (f"N/{portal}/battery/+/FirmwareVersion", 0),
            (f"N/{portal}/platform/0/Firmware/Installed/Version", 0),
        ]
        log.info("connected; subscribing %d topic patterns", len(topics))
        client.subscribe(topics)
        # mark Cerberus online + kick the GX into publishing. The keepalive
        # MUST be published on every (re)connect — paho's auto-reconnect calls
        # this handler again, so a broker bounce or network blip is
        # self-healing without intervention from the keepalive_loop.
        if self.own_topics:
            client.publish(self.availability_topic, b"online", qos=1, retain=True)
        # The keepalive doubles as a full-republish trigger: the GX dumps every
        # topic once in response. That is how rarely-changing values (shore
        # connected, AC input source) reach us at all — they are published on
        # change, and plugging in shore power may be days apart.
        self._publish_keepalive()
        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception:
                log.exception("on_ready callback raised")

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False
        # If we just dropped after a short-lived connection, that's a "soft"
        # failure too — count it toward rotation. A long-lived session means
        # the host is healthy and a single drop shouldn't push us off it.
        if self._connected_since is not None:
            session_age = time.time() - self._connected_since
            if session_age < STICKY_CONNECT_SECONDS:
                self._note_failed_attempt()
            else:
                self._attempts_since_last_success = 0
                self._rotation_suppressions = 0
        self._connected_since = None
        log.warning("disconnected (host=%s, will re-resolve and reconnect)",
                    self.current_host)
        if self._stop.is_set():
            return
        if self._forcing:
            # This disconnect is the one force_reconnect just triggered; it is
            # already rebuilding the session. Scheduling another reconnect here
            # would race it.
            return
        # Give paho's network loop a moment to settle before kicking a new
        # connect_async with a fresh address.
        threading.Timer(RECONNECT_GRACE_SECONDS, self._refresh_and_reconnect).start()

    def _refresh_and_reconnect(self) -> None:
        if self._stop.is_set():
            return
        try:
            self._retarget(self._resolve())
        except Exception:
            log.exception("refresh/reconnect failed; watchdog will retry")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.debug("non-json message on %s: %s", msg.topic, e)
            return

        value = payload.get("value") if isinstance(payload, dict) else None
        topic = msg.topic

        # Every Dc/0 message, for cerberus.fastcapture; the branches below fold
        # the ones they care about into state as usual.
        if self.on_dc is not None and "/Dc/0/" in topic:
            try:
                self.on_dc(topic, value)
            except Exception:
                log.exception("on_dc handler raised")

        if self.on_setting is not None:
            try:
                self.on_setting(topic, value)
            except Exception:
                log.exception("on_setting handler raised")
        # The settings tree is for on_setting alone (bar the time zone): paths
        # like .../BatteryLife/State would otherwise fall into the AC branch.
        if "/settings/0/" in topic and not topic.endswith("/Settings/System/TimeZone"):
            return

        if topic.endswith(f"/{self.cfg.primary_topic}"):
            if isinstance(value, list):
                try:
                    self.on_batteries(value)
                except Exception:
                    log.exception("on_batteries handler raised")
        elif topic.endswith("/heartbeat"):
            try:
                self.on_heartbeat(int(value) if value is not None else int(time.time()))
            except Exception:
                log.exception("on_heartbeat handler raised")
        elif "/gps/" in topic or topic.endswith("/Settings/System/TimeZone"):
            if self.on_location is not None:
                try:
                    self.on_location(topic, value)
                except Exception:
                    log.exception("on_location handler raised")
        # Before the AC branch, which claims anything ending /State — and
        # platform/0/Network/Wifi/State does.
        elif "/platform/0/Network/Wifi/" in topic:
            if self.on_wifi is not None:
                try:
                    self.on_wifi(topic, value)
                except Exception:
                    log.exception("on_wifi handler raised")
        # Before the AC branch, which claims anything ending /State or /Mode.
        # Neither of these does, but keeping the specific test first makes the
        # ordering intentional rather than incidental.
        elif "/temperature/" in topic and self.on_temperature is not None:
            try:
                self.on_temperature(topic, value)
            except Exception:
                log.exception("on_temperature handler raised")
        elif topic.endswith(("/Soh", "/Capacity")) and self.on_battery_health is not None:
            try:
                self.on_battery_health(topic, value)
            except Exception:
                log.exception("on_battery_health handler raised")
        elif "/Alarms/" in topic and self.on_alarm is not None:
            try:
                self.on_alarm(topic, value)
            except Exception:
                log.exception("on_alarm handler raised")
        # Solar is tested before AC: solarcharger/<inst>/State ends with
        # "/State" and would otherwise fall into the AC branch below.
        elif "/solarcharger/" in topic or topic.endswith("/Dc/Pv/Power"):
            if self.on_solar is not None:
                try:
                    self.on_solar(topic, value)
                except Exception:
                    log.exception("on_solar handler raised")
        elif ("/Ac/" in topic or topic.endswith(("/State", "/Mode"))
              or ("/vebus/" in topic and "/Dc/0/" in topic)):
            if self.on_ac is not None:
                try:
                    self.on_ac(topic, value)
                except Exception:
                    log.exception("on_ac handler raised")

    def _publish_keepalive(self, full_republish: bool = True) -> None:
        """Keep the GX publishing; optionally ask it to resend every topic.

        An empty payload makes the GX dump its whole tree: ~2050 messages,
        measured 2026-10-14 against ~15 msg/s of genuine change traffic. Sent
        every 30 s that was ~80% of everything the Cerbo pushed over its WiFi.
        suppress-republish only extends the keepalive timer.
        """
        topic = f"R/{self.cfg.portal_id}/keepalive"
        payload = b"" if full_republish else SUPPRESS_REPUBLISH_PAYLOAD
        self.client.publish(topic, payload=payload, qos=0, retain=False)
        if full_republish:
            self._last_full_republish = time.monotonic()

    def _keepalive_loop(self) -> None:
        # Periodic full republishes still run, just rarely: WiFi signal,
        # VE.Bus state, Soh and Capacity are published only on change, so
        # without one those tables would stop getting rows while values hold.
        period = self.cfg.keepalive_publish_seconds
        while not self._stop.wait(period):
            due = (time.monotonic() - self._last_full_republish
                   >= self.cfg.full_republish_seconds)
            try:
                self._publish_keepalive(full_republish=due)
            except Exception:
                log.exception("keepalive publish failed")
