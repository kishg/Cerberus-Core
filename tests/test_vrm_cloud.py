"""VRM cloud MQTT fallback (cerberus.vrm_cloud + Cerberus._cloud_tick).

Regression cover for 2026-10-20: the Cerbo came home on the van's Starlink
while this Mac was on the house WiFi, and the collector was blind for hours
although the Cerbo was online and VRM showed it live. No sockets or HTTP:
the account lookup and the client class are replaced.
"""

from __future__ import annotations

import ssl
from types import SimpleNamespace

import cerberus.__main__ as main_mod
from cerberus import vrm_cloud
from cerberus.config import MqttConfig
from cerberus.mqtt_client import CerboMqtt


def _cfg(**kw) -> MqttConfig:
    base = dict(host="192.168.1.60", hosts=("192.168.1.60", "venus.local"),
                port=1883, portal_id="0123456789ab", username="0123456789ab",
                password="pw", keepalive_publish_seconds=30,
                primary_topic="system/0/Batteries", publish_namespace="cerberus",
                ha_discovery_prefix="homeassistant", cloud_fallback=True,
                cloud_after_seconds=180)
    base.update(kw)
    return MqttConfig(**base)


def test_broker_host_matches_the_verified_shard():
    # Victron's rule, verified against a real installation on 2026-10-20: the
    # sum of the portal id's bytes mod 128. This placeholder id sums to 80.
    assert vrm_cloud.broker_host("0123456789ab") == "mqtt80.victronenergy.com"


def test_cloud_config_swaps_only_the_connection():
    c = vrm_cloud.cloud_config(_cfg(), "tok", "me@example.com")
    assert c.hosts == ("mqtt80.victronenergy.com",) and c.port == 8883
    assert c.username == "me@example.com" and c.password == "Token tok"
    assert c.portal_id == "0123456789ab" and c.primary_topic == "system/0/Batteries"


def test_tls_context_trusts_victron_ca_without_strict_x509():
    ctx = vrm_cloud.tls_context()
    assert vrm_cloud.CA_FILE.exists()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT


def test_read_only_client_sends_nothing_into_cerberus_namespace():
    """The relay ACL admits only the installation's tree: no LWT, no online."""
    c = CerboMqtt(_cfg(), on_batteries=lambda v: None, on_heartbeat=lambda v: None,
                  own_topics=False)
    assert not c.client._will
    sent = []
    c.client.publish = lambda topic, *a, **k: sent.append(topic)
    c.client.subscribe = lambda *a, **k: None
    c._on_connect(c.client, None, None, 0)
    assert sent == ["R/0123456789ab/keepalive"]


def test_tls_client_connects_by_hostname(monkeypatch):
    """The relay's certificate is for *.victronenergy.com; an IP never matches."""
    c = CerboMqtt(vrm_cloud.cloud_config(_cfg(), "tok", "me@example.com"),
                  on_batteries=lambda v: None, on_heartbeat=lambda v: None,
                  tls_context=vrm_cloud.tls_context(), own_topics=False)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: 1 / 0)
    assert c._resolve() == "mqtt80.victronenergy.com"


def test_local_client_still_publishes_availability():
    c = CerboMqtt(_cfg(), on_batteries=lambda v: None, on_heartbeat=lambda v: None)
    assert c.client._will
    sent = []
    c.client.publish = lambda topic, *a, **k: sent.append(topic)
    c.client.subscribe = lambda *a, **k: None
    c._on_connect(c.client, None, None, 0)
    assert sent[0] == "cerberus/availability"


# ---------------------------------------------------------------------------
# Cerberus._cloud_tick
# ---------------------------------------------------------------------------

class FakeCloud:
    instances: list["FakeCloud"] = []

    def __init__(self, cfg, **kw):
        self.cfg, self.kw = cfg, kw
        self.started = self.stopped = False
        FakeCloud.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _collector(monkeypatch, email=lambda tok: "me@example.com"):
    FakeCloud.instances = []
    monkeypatch.setattr(main_mod, "CerboMqtt", FakeCloud)
    monkeypatch.setattr(main_mod.vrm_cloud, "account_email", email)
    noop = lambda *a: None
    return SimpleNamespace(
        cfg=SimpleNamespace(mqtt=_cfg()), mqtt=SimpleNamespace(is_connected=False),
        vrm_token="tok", cloud=None, _local_down_since=None, _cloud_next_try=0.0,
        _on_batteries=noop, _on_heartbeat=noop, _on_alarm=noop, _on_ac=noop,
        _on_solar=noop, _on_battery_health=noop, _on_temperature=noop, _on_wifi=noop,
        _on_location=noop,
    )


def tick(c, t):
    main_mod.Cerberus._cloud_tick(c, now=t)


def test_starts_only_after_the_local_link_has_been_down_long_enough(monkeypatch):
    c = _collector(monkeypatch)
    tick(c, 1000)
    tick(c, 1179)
    assert c.cloud is None
    tick(c, 1180)
    assert c.cloud is FakeCloud.instances[0] and c.cloud.started
    assert c.cloud.cfg.port == 8883 and c.cloud.kw["own_topics"] is False
    assert "on_ready" not in c.cloud.kw     # HA discovery belongs to the local link
    tick(c, 1300)
    assert len(FakeCloud.instances) == 1


def test_stops_as_soon_as_local_reconnects(monkeypatch):
    c = _collector(monkeypatch)
    tick(c, 0)
    tick(c, 200)
    cloud = c.cloud
    c.mqtt.is_connected = True
    tick(c, 210)
    assert cloud.stopped and c.cloud is None and c._local_down_since is None


def test_a_brief_local_blip_never_starts_it(monkeypatch):
    c = _collector(monkeypatch)
    tick(c, 0)
    c.mqtt.is_connected = True
    tick(c, 100)
    c.mqtt.is_connected = False
    tick(c, 150)
    tick(c, 250)                 # 100 s into the second outage, not 250
    assert c.cloud is None


def test_failed_start_backs_off(monkeypatch):
    def no_internet(tok):
        raise OSError("no route")
    c = _collector(monkeypatch, email=no_internet)
    tick(c, 0)
    tick(c, 200)
    assert c.cloud is None and c._cloud_next_try == 200 + main_mod.CLOUD_RETRY_SECONDS
    monkeypatch.setattr(main_mod.vrm_cloud, "account_email", lambda tok: "me@example.com")
    tick(c, 300)
    assert c.cloud is None
    tick(c, 200 + main_mod.CLOUD_RETRY_SECONDS)
    assert c.cloud is not None
