"""Direct publishing to HA's broker (cerberus.ha_mqtt).

Regression cover for 2026-10-20: Cerberus's topics reached HA only through
the Cerbo and the ha-stack bridge, so with the Cerbo on another network the
panel froze on retained values (a 1.7 kW draw shown for hours at ~40 W).
"""

from __future__ import annotations

from cerberus.ha_mqtt import HaBroker
from cerberus.publisher import Publisher


def _broker(on_ready=None):
    b = HaBroker("127.0.0.1", 1883, "cerberus/availability", on_ready=on_ready)
    sent = []
    b.client.publish = lambda topic, payload=None, qos=0, retain=False: sent.append(
        (topic, payload, retain))
    return b, sent


def test_lwt_marks_cerberus_offline_on_ha_broker():
    b, _ = _broker()
    assert b.client._will
    assert b.client._will_topic == b"cerberus/availability"
    assert b.client._will_payload == b"offline"


def test_every_connect_reruns_on_ready():
    calls = []
    b, _ = _broker(on_ready=lambda: calls.append(1))
    b._on_connect(b.client, None, None, 0)
    b._on_disconnect(b.client, None)
    b._on_connect(b.client, None, None, 0)
    assert calls == [1, 1] and b.connected


def test_failed_connect_does_not_fire_on_ready():
    calls = []
    b, _ = _broker(on_ready=lambda: calls.append(1))
    b._on_connect(b.client, None, None, 5)
    assert calls == [] and not b.connected


def test_availability_follows_the_feed_not_the_connection():
    b, sent = _broker()
    p = Publisher(mqtt=b, namespace="cerberus", discovery_prefix="homeassistant",
                  pack_ids=(1, 2, 3))
    p.publish_availability(False)
    p.publish_availability(True)
    assert sent == [("cerberus/availability", b"offline", True),
                    ("cerberus/availability", b"online", True)]
