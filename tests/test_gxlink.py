"""The GX link in words (cerberus.gxlink, #66): since when, why, and what Victron's cloud is getting.

The case that read as working before: the fallback connected and subscribed, but the GX was gone
from VRM too, so the relay sent only its retained Serial.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from types import SimpleNamespace

from cerberus import gxlink, healthreport, live
from cerberus.ac import AcState
from cerberus.mqtt_client import CerboMqtt
from cerberus.solar import SolarState

from .test_vrm_cloud import _cfg

P = "0123456789ab"


def watch(enabled=True, token=True):
    return gxlink.LinkWatch(enabled, token, 180)


def test_link_up_says_nothing_more():
    w = watch()
    w.lan_heartbeat(now=1000)
    assert w.view(1030) == {"lan": "ok", "lan_last": 1000, "cause": None, "fallback": None, "fallback_last": None}


def test_retained_serial_alone_is_not_the_cloud_working():
    w = watch()
    w.lan_heartbeat(now=1000)
    w.cloud_up(now=1200)
    w.cloud_connected = True
    w.cloud_message(f"N/{P}/system/0/Serial", retained=True, now=1201)
    w.cloud_message(f"N/{P}/system/0/Serial", retained=False, now=1202)     # the keepalive's answer
    w.cloud_message(f"N/{P}/system/0/Batteries", retained=True, now=1203)  # the relay's memory, not the GX
    v = w.view(1300)
    assert (v["lan"], v["lan_last"], v["fallback"], v["fallback_last"]) == ("down", 1000, "no_data", None)


def test_fresh_data_from_the_cloud_is_delivering_until_it_stops():
    w = watch()
    w.lan_heartbeat(now=1000)
    w.cloud_up(now=1200)
    w.cloud_connected = True
    w.cloud_message(f"N/{P}/system/0/Batteries", retained=False, now=1210)
    assert w.view(1220)["fallback"] == "delivering"
    assert w.view(1210 + gxlink.FRESH_S)["fallback"] == "no_data"


def test_fallback_states_before_any_data():
    assert watch(enabled=False).view(5000)["fallback"] == "off"
    assert watch(token=False).view(5000)["fallback"] == "no_token"
    w = watch()
    assert w.view(5000)["fallback"] == "waiting"
    w.cloud_up(now=5000)
    assert w.view(5001)["fallback"] == "connecting"
    w.cloud_connected = True
    assert w.view(5002)["fallback"] == "no_data"
    w.cloud_down()
    assert w.view(5003)["fallback"] == "waiting"
    assert w.view(5003)["lan_last"] is None                  # never heard since the start


def test_cause_is_probed_at_most_once_a_minute_and_cleared_by_a_heartbeat():
    w, calls = watch(), []
    probe = lambda ip, port: calls.append(ip) or gxlink.NO_ANSWER  # noqa: E731
    w.maybe_probe(1000, "192.0.2.60", 1883, probe_fn=probe)
    w.maybe_probe(1030, "192.0.2.60", 1883, probe_fn=probe)
    assert calls == ["192.0.2.60"] and w.view(1030)["cause"] == "no_answer"
    w.maybe_probe(1061, "192.0.2.60", 1883, connected=True, probe_fn=probe)
    assert w.view(1061)["cause"] == "silent" and len(calls) == 1
    w.lan_heartbeat(now=1070)
    assert w.view(1071)["cause"] is None


def test_probe_tells_refused_from_no_answer():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                 # nothing listens there now
    assert gxlink.probe("127.0.0.1", port) == gxlink.REFUSED
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        assert gxlink.probe("127.0.0.1", srv.getsockname()[1]) is None
    finally:
        srv.close()
    assert gxlink.probe(None, 1883) is None


def test_cloud_client_reports_every_message_and_ignores_a_retained_heartbeat():
    beats, seen = [], []
    c = CerboMqtt(_cfg(), on_batteries=lambda v: None, on_heartbeat=beats.append, own_topics=False,
                  on_any=lambda t, r: seen.append((t, r)))
    msg = lambda topic, retain: SimpleNamespace(topic=topic, retain=retain, payload=json.dumps({"value": 1}).encode())  # noqa: E731
    c._on_message(c.client, None, msg(f"N/{P}/heartbeat", True))
    assert beats == [] and seen == [(f"N/{P}/heartbeat", True)]
    c._on_message(c.client, None, msg(f"N/{P}/heartbeat", False))
    assert beats == [1]
    local = CerboMqtt(_cfg(), on_batteries=lambda v: None, on_heartbeat=beats.append)
    local._on_message(local.client, None, msg(f"N/{P}/heartbeat", True))    # the local link: unchanged
    assert beats == [1, 1]


def test_snapshot_and_health_report_carry_it_without_addresses():
    w = watch()
    w.lan_heartbeat(now=1000)
    w.cause = gxlink.NO_ANSWER
    view = w.view(2800)
    snap = live.snapshot(2800, False, None, SolarState(), AcState(), gx=view)
    assert snap["gx"]["fallback"] == "waiting"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO kv VALUES ('live.snapshot', ?)", (live.dumps(snap),))
    assert healthreport.gx_link(conn, 2800) == {"lan": "down", "cause": "no_answer", "down_s": 1800,
                                                "fallback": "waiting"}
    assert healthreport.gx_link(sqlite3.connect(":memory:"), 2800) is None    # no table yet: nothing


def test_collector_tells_ha_only_when_the_link_changes():
    import cerberus.__main__ as main_mod
    sent = []
    c = SimpleNamespace(gx=watch(), cloud=None, _local_down_since=None, _gx_said=None, gx_view=None,
                        mqtt=SimpleNamespace(target_ip="192.0.2.60", is_connected=False),
                        cfg=SimpleNamespace(mqtt=SimpleNamespace(port=1883)),
                        publisher=SimpleNamespace(publish_cerbo_link=sent.append))
    c.gx._probed = 10_000                                      # no real probe in a unit test
    c.gx.lan_heartbeat(now=1000)
    main_mod.Cerberus._gx_tick(c, now=1010)
    main_mod.Cerberus._gx_tick(c, now=1020)
    assert [v["lan"] for v in sent] == ["ok"]
    main_mod.Cerberus._gx_tick(c, now=1100)                   # the heartbeat is a minute old
    assert [v["lan"] for v in sent] == ["ok", "down"] and c.gx_view["fallback"] == "waiting"
