"""Van position and local time (step 4): cerberus.location, its MQTT routing,
the weather fetch, and what a "day" means to the nightly jobs."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cerberus import config as cfg_mod
from cerberus import drives
from cerberus import solar_path as sp
from cerberus.__main__ import Cerberus
from cerberus.db import Db
from cerberus.location import (
    GPS_STALE_SECONDS,
    KV_POSITION,
    KV_TIMEZONE,
    VanLocation,
    day_start,
    next_day,
)

NY = ZoneInfo("America/New_York")
DENVER = ZoneInfo("America/Denver")
GPS = "N/abc/gps/0"
TZ_TOPIC = "N/abc/settings/0/Settings/System/TimeZone"
HOME = (40.8662, -74.3805)


def _fix(loc, lat, lon, now, fix=1):
    loc.update(f"{GPS}/Fix", fix, now)
    loc.update(f"{GPS}/Position/Latitude", lat, now)
    loc.update(f"{GPS}/Position/Longitude", lon, now)


# --- position -------------------------------------------------------------------

def test_no_receiver_uses_the_configured_position():
    """This rig: no gps service on the GX at all."""
    assert VanLocation({}).position(*HOME, now=0) == (*HOME, "config")


def test_a_live_fix_wins():
    loc = VanLocation({})
    _fix(loc, 44.43, -110.59, now=100)
    assert loc.position(*HOME, now=200) == (44.43, -110.59, "gps")


def test_no_fix_means_no_gps_position():
    loc = VanLocation({})
    _fix(loc, 44.43, -110.59, now=100, fix=0)
    assert loc.position(*HOME, now=200)[2] == "config"


def test_stale_fix_falls_back_to_the_last_one_on_record():
    kv = {}
    loc = VanLocation(kv)
    _fix(loc, 44.43, -110.59, now=100)
    assert loc.position(*HOME, now=100 + GPS_STALE_SECONDS + 1) == (44.43, -110.59, "gps_last")
    # And across a restart, before any GPS message arrives.
    assert VanLocation(kv).position(*HOME, now=10**9) == (44.43, -110.59, "gps_last")


def test_parked_jitter_does_not_rewrite_kv():
    kv = {}
    loc = VanLocation(kv)
    _fix(loc, 44.43, -110.59, now=100)
    first = kv[KV_POSITION]
    _fix(loc, 44.4301, -110.5902, now=400)
    assert kv[KV_POSITION] == first
    _fix(loc, 44.50, -110.59, now=700)          # ~8 km on
    assert json.loads(kv[KV_POSITION])["lat"] == 44.50


def test_non_location_topics_are_ignored():
    loc = VanLocation({})
    assert loc.update("N/abc/settings/0/Settings/Gps/Format", 0) is False


# --- time zone ------------------------------------------------------------------

def test_gx_zone_is_remembered():
    kv = {}
    VanLocation(kv).update(TZ_TOPIC, "America/Indianapolis")
    assert kv[KV_TIMEZONE] == "America/Indianapolis"
    assert VanLocation(kv).timezone() == ZoneInfo("America/Indianapolis")


def test_config_zone_beats_the_gx():
    loc = VanLocation({KV_TIMEZONE: "America/Indianapolis"})
    assert loc.timezone("America/Denver") == DENVER


def test_unknown_or_missing_zone_is_host_local():
    assert VanLocation({}).timezone() is None
    assert VanLocation({KV_TIMEZONE: "Mars/Olympus"}).timezone() is None


def test_indianapolis_is_new_jerseys_clock_all_year():
    """Why this rig's switch from host time to the GX zone changes nothing."""
    indy = ZoneInfo("America/Indianapolis")
    for ts in range(1767225600, 1798761600, 86400 * 7):      # all of 2026, weekly
        assert datetime.fromtimestamp(ts, indy).utcoffset() == datetime.fromtimestamp(ts, NY).utcoffset()


@pytest.mark.parametrize("body, ok", [("      timezone: America/Denver\n", True),
                                      ("      timezone: Eastern\n", False)])
def test_config_zone_is_validated(tmp_path, monkeypatch, body, ok):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw")
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
        mqtt: {host: venus.local, portal_id: abc}
        packs: {uc1: 1}
        storage: {sqlite_path: /tmp/x.db}
        system:
    """) + body)
    if ok:
        assert cfg_mod.load(p).timezone == "America/Denver"
    else:
        with pytest.raises(cfg_mod.ConfigError, match="system.timezone"):
            cfg_mod.load(p)


# --- local days -----------------------------------------------------------------

def test_dst_end_day_is_25_hours():
    """2026-11-01: US clocks fall back, so that local day is 25 h long; a
    start + 86400 end would cut off its last hour."""
    start, end = day_start("2026-11-01", NY), day_start(next_day("2026-11-01"), NY)
    assert end - start == 25 * 3600


def test_a_day_is_the_vans_day():
    assert day_start("2026-10-31", DENVER) - day_start("2026-10-31", NY) == 2 * 3600


def test_nightly_processing_hour_is_van_local():
    """21:30 in New Jersey is 19:30 in Denver: a van in Denver has not had
    its 21:00 yet, so the day is not ready."""
    db = Db(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.conn.execute("INSERT INTO solar_samples (ts, charge_current) VALUES (?, 1.0)",
                    (day_start("2026-10-30", NY),))
    now = int(datetime(2026, 10, 31, 21, 30, tzinfo=NY).timestamp())
    assert sp._days_to_process(db, now, "2026-10-30", 21, NY) == ["2026-10-31"]
    assert sp._days_to_process(db, now, "2026-10-30", 21, DENVER) == []


def test_report_times_are_van_local():
    ts = int(datetime(2026, 10, 31, 13, 5, tzinfo=NY).timestamp())
    d = drives.Drive(ts, ts + 3600, "electrical", {"duration_min": 60.0})
    assert drives.summary_line(d, NY).startswith("Drive 10-31 13:05")
    assert drives.summary_line(d, DENVER).startswith("Drive 10-31 11:05")


# --- routing and weather ----------------------------------------------------------

def test_mqtt_routes_gps_and_zone_to_on_location():
    from cerberus.config import MqttConfig
    from cerberus.mqtt_client import CerboMqtt
    cfg = MqttConfig(host="h", hosts=("h",), port=1883, portal_id="abc", username="abc",
                     password="pw", keepalive_publish_seconds=30,
                     primary_topic="system/0/Batteries", publish_namespace="cerberus",
                     ha_discovery_prefix="homeassistant")
    seen = []
    m = CerboMqtt(cfg, on_batteries=lambda v: None, on_heartbeat=lambda t: None,
                  on_location=lambda t, v: seen.append((t, v)),
                  on_ac=lambda t, v: seen.append(("ac", t)))
    for topic, value in ((f"{GPS}/Position/Latitude", 44.43), (f"{GPS}/Fix", 1),
                         (TZ_TOPIC, "America/Denver")):
        m._on_message(None, None, SimpleNamespace(
            topic=topic, payload=json.dumps({"value": value}).encode()))
    assert [t for t, _ in seen] == [f"{GPS}/Position/Latitude", f"{GPS}/Fix", TZ_TOPIC]


def test_weather_is_fetched_where_the_van_is(monkeypatch, tmp_path):
    from cerberus import __main__ as main_mod
    from cerberus.weather import WeatherSample
    calls = []
    monkeypatch.setattr(main_mod, "fetch_weather", lambda lat, lon, ua, timeout: (
        calls.append((lat, lon)) or WeatherSample(50.0, 20.0, 60.0, 5.0)))
    c = Cerberus.__new__(Cerberus)
    c.cfg = SimpleNamespace(weather=SimpleNamespace(
        latitude=HOME[0], longitude=HOME[1], user_agent="t", interval_seconds=900,
        request_timeout_seconds=10))
    c.db = Db(str(tmp_path / "w.db"))
    c.location = VanLocation(c.db.kv_mapping())
    c.last_weather_tick_ts = 0
    c._weather_tick()
    _fix(c.location, 44.43, -110.59, now=int(datetime.now().timestamp()))
    c.last_weather_tick_ts = 0
    c._weather_tick()
    assert calls == [HOME, (44.43, -110.59)]
    rows = c.db.conn.execute(
        "SELECT lat, lon, position_source FROM weather_samples ORDER BY ts").fetchall()
    assert rows[-1] == (44.43, -110.59, "gps")
