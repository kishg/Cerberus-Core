"""AC power readings must go unavailable, not stale.

Regression cover for the 2026-08-08 stale-retained bug: the inverter was
switched off at ~15:42, the Cerbo began publishing null for AC consumption and
inverter output, and publish_ac_state skipped those fields. The retained topics
kept serving the last live values, so HA reported "AC Consumption 178W" for the
next 40 hours while nothing at all was running.

Silence is indistinguishable from a fresh reading on a retained topic, so a
missing value has to be published as an explicit availability change.
"""

from __future__ import annotations

from cerberus.ac import AcState
from cerberus.publisher import Publisher

P = "N/0123456789ab"
VEBUS = f"{P}/vebus/276"


class FakeMqtt:
    """Records publishes; last write wins, mirroring retained-topic semantics."""

    def __init__(self):
        self.topics: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def publish(self, topic, payload, retain=False, qos=0):
        if isinstance(payload, bytes):
            payload = payload.decode()
        self.topics[topic] = payload
        self.calls.append((topic, payload))


def _pub():
    m = FakeMqtt()
    return m, Publisher(mqtt=m, namespace="cerberus",
                        discovery_prefix="homeassistant", pack_ids=(1, 2, 3))


def _feed(ac, pairs):
    for t, v in pairs:
        ac.update(t, v)


INVERTER_ON = [
    (f"{P}/system/0/Ac/Consumption/L1/Power", 178),
    (f"{VEBUS}/Ac/Out/L1/P", 181),
    (f"{VEBUS}/Ac/ActiveIn/P", 0),
    (f"{VEBUS}/State", 9),
]


def test_live_values_publish_with_online_flag():
    m, p = _pub()
    ac = AcState()
    _feed(ac, INVERTER_ON)
    p.publish_ac_state(ac)
    assert m.topics["cerberus/ac/consumption_power"] == "178.0"
    assert m.topics["cerberus/ac/consumption_power/avail"] == "online"
    assert m.topics["cerberus/ac/output_power"] == "181.0"
    assert m.topics["cerberus/ac/output_power/avail"] == "online"


def test_inverter_off_marks_power_unavailable():
    """The actual regression: values go null, entity must go unavailable."""
    m, p = _pub()
    ac = AcState()
    _feed(ac, INVERTER_ON)
    p.publish_ac_state(ac)

    # inverter switched off — the Cerbo starts publishing null
    _feed(ac, [(f"{P}/system/0/Ac/Consumption/L1/Power", None),
               (f"{VEBUS}/Ac/Out/L1/P", None),
               (f"{VEBUS}/State", 0)])
    m.calls.clear()
    p.publish_ac_state(ac)

    assert m.topics["cerberus/ac/consumption_power/avail"] == "offline"
    assert m.topics["cerberus/ac/output_power/avail"] == "offline"
    # the stale number must not be republished
    assert not any(t == "cerberus/ac/consumption_power" for t, _ in m.calls)


def test_recovery_republishes_before_going_online():
    """Coming back must not leave HA briefly showing the old retained value."""
    m, p = _pub()
    ac = AcState()
    _feed(ac, INVERTER_ON)
    p.publish_ac_state(ac)
    _feed(ac, [(f"{P}/system/0/Ac/Consumption/L1/Power", None)])
    p.publish_ac_state(ac)
    assert m.topics["cerberus/ac/consumption_power/avail"] == "offline"

    _feed(ac, [(f"{P}/system/0/Ac/Consumption/L1/Power", 240)])
    m.calls.clear()
    p.publish_ac_state(ac)
    order = [t for t, _ in m.calls if t.startswith("cerberus/ac/consumption_power")]
    assert order == ["cerberus/ac/consumption_power",
                     "cerberus/ac/consumption_power/avail"]
    assert m.topics["cerberus/ac/consumption_power"] == "240.0"
    assert m.topics["cerberus/ac/consumption_power/avail"] == "online"


def test_independent_gating_per_field():
    """Input power is reported as a real 0 while consumption is null — they
    must not share one availability flag."""
    m, p = _pub()
    ac = AcState()
    _feed(ac, [(f"{VEBUS}/Ac/ActiveIn/P", 0),
               (f"{P}/system/0/Ac/Consumption/L1/Power", None)])
    p.publish_ac_state(ac)
    assert m.topics["cerberus/ac/input_power"] == "0.0"
    assert m.topics["cerberus/ac/input_power/avail"] == "online"
    assert m.topics["cerberus/ac/consumption_power/avail"] == "offline"


def test_zero_watts_is_available_not_missing():
    m, p = _pub()
    ac = AcState()
    _feed(ac, [(f"{P}/system/0/Ac/Consumption/L1/Power", 0)])
    p.publish_ac_state(ac)
    assert m.topics["cerberus/ac/consumption_power"] == "0.0"
    assert m.topics["cerberus/ac/consumption_power/avail"] == "online"


def test_discrete_facts_are_not_gated():
    """Inverter state and shore-connected stay true until they change, so
    they keep the plain global availability."""
    m, p = _pub()
    ac = AcState()
    _feed(ac, [(f"{VEBUS}/State", 0), (f"{P}/system/0/Ac/In/0/Connected", 0)])
    p.publish_ac_state(ac)
    assert m.topics["cerberus/ac/inverter_state"] == "off"
    assert m.topics["cerberus/ac/shore_connected"] == "off"
    assert "cerberus/ac/inverter_state/avail" not in m.topics


# ---------------------------------------------------------------------------
# discovery payloads
# ---------------------------------------------------------------------------

def _configs(m):
    import json
    return {t: json.loads(v) for t, v in m.topics.items() if t.endswith("/config")}


def test_gated_sensors_declare_both_availability_sources():
    m, p = _pub()
    p.publish_discovery()
    cfg = _configs(m)["homeassistant/sensor/cerberus/ac_consumption_power/config"]
    assert cfg["availability_mode"] == "all"
    topics = [a["topic"] for a in cfg["availability"]]
    assert "cerberus/availability" in topics
    assert "cerberus/ac/consumption_power/avail" in topics
    # mutually exclusive with the singular form
    assert "availability_topic" not in cfg


def test_ungated_sensors_keep_the_simple_form():
    m, p = _pub()
    p.publish_discovery()
    cfg = _configs(m)["homeassistant/sensor/cerberus/soc_spread/config"]
    assert cfg["availability_topic"] == "cerberus/availability"
    assert "availability" not in cfg
