"""Publishing straight to Home Assistant's own broker.

Why this exists
---------------
Cerberus's state, events and discovery used to be published to the Cerbo's
broker and reach HA through the ha-stack mosquitto bridge. That chain needs
the LAN link to the Cerbo twice over (collector -> Cerbo, bridge -> Cerbo),
so whenever the Cerbo was on another network the HA panel froze on retained
values and looked healthy. On 2026-10-20 it showed a 1.7 kW bank draw for
hours while the real draw was ~40 W — the collector, by then on the VRM cloud
feed, had the right numbers and nowhere to send them.

HA's mosquitto listens on this machine, so publishing to it directly depends
only on the collector being alive. The bridge keeps carrying the handful of
raw ``N/...`` Victron topics HA reads itself; it no longer carries
``cerberus/#`` or ``homeassistant/#``.

Availability (``<ns>/availability``) now means "the collector is running and
its Cerbo feed is fresh", from whichever source: the LWT covers the collector
dying, and the caller flips it when the heartbeat goes stale.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger(__name__)


class NoBroker:
    """Stands in for HaBroker when the Home Assistant integration is off
    (ha_mqtt.enabled false): the publisher keeps calling, nothing is sent."""

    connected = False

    def start(self) -> None:
        log.info("Home Assistant integration off (ha_mqtt.enabled); not publishing to HA")

    def stop(self) -> None:
        pass

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        pass


class HaBroker:
    """Publish-only client for the local HA broker. paho reconnects on its own;
    on every (re)connect ``on_ready`` re-sends discovery and availability,
    since a broker restart without persistence would otherwise lose them."""

    def __init__(self, host: str, port: int, availability_topic: str,
                 on_ready: Callable[[], None] | None = None) -> None:
        self.host, self.port = host, port
        self.availability_topic = availability_topic
        self.on_ready = on_ready
        self.connected = False
        self.client = mqtt.Client(CallbackAPIVersion.VERSION2,
                                  client_id=f"cerberus-ha-{int(time.time())}")
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.will_set(availability_topic, payload=b"offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        log.info("HA broker: connecting to %s:%d", self.host, self.port)
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        try:
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
            log.error("HA broker connect failed: %s", reason_code)
            return
        self.connected = True
        log.info("HA broker connected (%s:%d)", self.host, self.port)
        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception:
                log.exception("HA on_ready callback raised")

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.connected = False
        log.warning("HA broker disconnected; paho will reconnect")
