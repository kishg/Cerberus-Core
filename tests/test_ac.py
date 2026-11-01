"""AC-side topic parsing and aggregation.

Topic strings and payload shapes here are taken from live captures off the
Cerbo (portal 0123456789ab, MultiPlus-II at vebus instance 276) on
2026-08-08, including the null-on-absent-phase behaviour that motivated
treating null as "no reading" rather than zero.
"""

from __future__ import annotations

from cerberus.ac import AcState

P = "N/0123456789ab"
VEBUS = f"{P}/vebus/276"


def _feed(ac: AcState, pairs) -> None:
    for topic, value in pairs:
        ac.update(topic, value)


# ---------------------------------------------------------------------------
# consumption — the "what is the kettle drawing" number
# ---------------------------------------------------------------------------

def test_single_phase_consumption():
    ac = AcState()
    assert ac.update(f"{P}/system/0/Ac/Consumption/L1/Power", 143) is True
    assert ac.total_consumption_w == 143


def test_consumption_sums_across_phases():
    ac = AcState()
    _feed(ac, [
        (f"{P}/system/0/Ac/Consumption/L1/Power", 143),
        (f"{P}/system/0/Ac/Consumption/L2/Power", 200),
        (f"{P}/system/0/Ac/Consumption/L3/Power", 57),
    ])
    assert ac.total_consumption_w == 400


def test_null_phases_are_skipped_not_zeroed():
    """L2/L3 publish null on this single-phase rig."""
    ac = AcState()
    _feed(ac, [
        (f"{P}/system/0/Ac/Consumption/L1/Power", 143),
        (f"{P}/system/0/Ac/Consumption/L2/Power", None),
        (f"{P}/system/0/Ac/Consumption/L3/Power", None),
    ])
    assert ac.total_consumption_w == 143


def test_no_data_is_none_not_zero():
    """Absent readings must stay distinguishable from a real 0 W."""
    ac = AcState()
    assert ac.total_consumption_w is None
    assert ac.total_input_w is None
    assert ac.total_output_w is None
    assert ac.has_data is False


def test_zero_is_a_real_reading():
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/Consumption/L1/Power", 0)
    assert ac.total_consumption_w == 0
    assert ac.has_data is True


def test_phase_going_null_stops_contributing():
    """Shore unplugged -> the phase nulls out. The last live watt figure must
    not stick around inflating the total."""
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/ActiveIn/L1/Power", 1200)
    assert ac.total_input_w == 1200
    ac.update(f"{P}/system/0/Ac/ActiveIn/L1/Power", None)
    assert ac.total_input_w is None


def test_kettle_sized_load():
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/Consumption/L1/Power", 1487)
    assert ac.total_consumption_w == 1487


# ---------------------------------------------------------------------------
# inverter output / input
# ---------------------------------------------------------------------------

def test_vebus_output_power():
    ac = AcState()
    ac.update(f"{VEBUS}/Ac/Out/L1/P", 130)
    assert ac.total_output_w == 130


def test_vebus_input_total_preferred_over_phase_sum():
    """Ac/ActiveIn/P is measured; the phase sum is derived. Prefer measured."""
    ac = AcState()
    _feed(ac, [
        (f"{VEBUS}/Ac/ActiveIn/L1/P", 500),
        (f"{VEBUS}/Ac/ActiveIn/P", 1234),
    ])
    assert ac.total_input_w == 1234


def test_output_voltage_tracked():
    ac = AcState()
    ac.update(f"{VEBUS}/Ac/Out/L1/V", 119.7)
    assert ac.output_v[1] == 119.7


def test_unhandled_vebus_field_still_claimed():
    """Frequency is a topic we subscribe to but don't surface — it should be
    reported as handled so it isn't logged as unknown."""
    ac = AcState()
    assert ac.update(f"{VEBUS}/Ac/Out/L1/F", 60.0) is True


# ---------------------------------------------------------------------------
# shore power + inverter state
# ---------------------------------------------------------------------------

def test_shore_connected_from_system_ac_input():
    """The real path on this firmware. A full topic dump on 2026-08-08 showed
    system/0/Ac/ActiveIn/Connected does not exist — only Ac/In/<idx>."""
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/In/0/Connected", 0)
    assert ac.shore_connected is False
    ac.update(f"{P}/system/0/Ac/In/0/Connected", 1)
    assert ac.shore_connected is True


def test_shore_connected_any_input_counts():
    ac = AcState()
    _feed(ac, [(f"{P}/system/0/Ac/In/0/Connected", 0),
               (f"{P}/system/0/Ac/In/1/Connected", 1)])
    assert ac.shore_connected is True


def test_absent_input_nulls_out_and_is_ignored():
    """In/1 publishes null on a rig with one AC input."""
    ac = AcState()
    _feed(ac, [(f"{P}/system/0/Ac/In/0/Connected", 0),
               (f"{P}/system/0/Ac/In/1/Connected", None)])
    assert ac.shore_connected is False


def test_shore_connected_falls_back_to_vebus():
    ac = AcState()
    ac.update(f"{VEBUS}/Ac/ActiveIn/Connected", 1)
    assert ac.shore_connected is True


def test_system_inputs_win_over_vebus():
    ac = AcState()
    _feed(ac, [(f"{VEBUS}/Ac/ActiveIn/Connected", 1),
               (f"{P}/system/0/Ac/In/0/Connected", 0)])
    assert ac.shore_connected is False


def test_shore_unknown_is_none_not_false():
    ac = AcState()
    assert ac.shore_connected is None


def test_inverter_state_label():
    ac = AcState()
    ac.update(f"{VEBUS}/State", 9)
    assert ac.inverter_state == 9
    assert ac.inverter_state_label == "inverting"


def test_unknown_inverter_state_is_labelled_not_dropped():
    ac = AcState()
    ac.update(f"{VEBUS}/State", 77)
    assert ac.inverter_state_label == "unknown (77)"


def test_charging_from_ac_requires_both_shore_and_charge_state():
    ac = AcState()
    _feed(ac, [(f"{VEBUS}/State", 9),  # inverting, off-grid
               (f"{P}/system/0/Ac/In/0/Connected", 0)])
    assert ac.is_charging_from_ac is False

    # plugged in but still passthru — not charging
    ac.update(f"{P}/system/0/Ac/In/0/Connected", 1)
    ac.update(f"{VEBUS}/State", 8)
    assert ac.is_charging_from_ac is False

    # bulk charge on shore power
    ac.update(f"{VEBUS}/State", 3)
    assert ac.is_charging_from_ac is True


def test_input_source_recorded():
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/ActiveIn/Source", 240)
    assert ac.input_source == 240


# ---------------------------------------------------------------------------
# routing hygiene
# ---------------------------------------------------------------------------

def test_non_ac_topics_are_rejected():
    """Battery topics must not be swallowed by the AC handler."""
    ac = AcState()
    assert ac.update(f"{P}/system/0/Dc/Battery/Power", -74) is False
    assert ac.update(f"{P}/battery/1/Soc", 98) is False
    assert ac.update(f"{P}/heartbeat", 1) is False
    assert ac.has_data is False


def test_malformed_topic_ignored():
    ac = AcState()
    assert ac.update("garbage", 1) is False
    assert ac.update("", 1) is False


def test_bool_payload_not_treated_as_number():
    ac = AcState()
    ac.update(f"{P}/system/0/Ac/Consumption/L1/Power", True)
    assert ac.total_consumption_w is None


def test_realistic_van_snapshot():
    """The live 2026-08-08 08:14 reading: inverting off-battery, no shore."""
    ac = AcState()
    _feed(ac, [
        (f"{P}/system/0/Ac/Consumption/L1/Power", 134),
        (f"{P}/system/0/Ac/Consumption/L2/Power", None),
        (f"{P}/system/0/Ac/ActiveIn/L1/Power", None),
        (f"{P}/system/0/Ac/In/0/Connected", 0),
        (f"{P}/system/0/Ac/In/0/Source", 3),
        (f"{P}/system/0/Ac/In/1/Connected", None),
        (f"{VEBUS}/Ac/ActiveIn/Connected", 0),
        (f"{VEBUS}/Ac/Out/L1/P", 130),
        (f"{VEBUS}/Ac/ActiveIn/P", 0),
        (f"{VEBUS}/State", 9),
        (f"{VEBUS}/Mode", 3),
    ])
    assert ac.total_consumption_w == 134
    assert ac.total_output_w == 130
    assert ac.total_input_w == 0
    assert ac.shore_connected is False
    assert ac.inverter_state_label == "inverting"
    assert ac.is_charging_from_ac is False
    assert ac.has_data is True


# ---------------------------------------------------------------------------
# inverter off — every AC power path goes null
#
# Regression cover for 2026-09-17. With the inverter switched off the Cerbo
# publishes null for the whole AC power tree; only vebus/<inst>/State carries
# a real value. Verified by explicit R/ reads against the live Cerbo that
# evening:
#
#   system/0/Ac/Consumption/L1/Power   null
#   system/0/Ac/ActiveIn/L1/Power      null
#   vebus/276/Ac/ActiveIn/P            null
#   vebus/276/Ac/Out/L1/P              null
#   system/0/Ac/In/0/Connected         null
#   vebus/276/State                    0
# ---------------------------------------------------------------------------

def test_input_total_is_cleared_by_null():
    """A latched total outranks correctly-cleared phases.

    total_input_w *prefers* the vebus total over the per-phase sum, so this
    field sticking is worse than an ordinary stale cache: it keeps winning
    after the phases have properly dropped out. Shore was unplugged at 02:00
    on 2026-09-17 and ac_samples went on reporting input_w = 0.0 until the
    collector restarted 15 hours later.
    """
    ac = AcState()
    _feed(ac, [
        (f"{VEBUS}/Ac/ActiveIn/P", 1076),
        (f"{P}/system/0/Ac/ActiveIn/L1/Power", 1076),
    ])
    assert ac.total_input_w == 1076

    # Shore unplugged: the whole input tree goes null.
    _feed(ac, [
        (f"{VEBUS}/Ac/ActiveIn/P", None),
        (f"{P}/system/0/Ac/ActiveIn/L1/Power", None),
    ])
    assert ac.total_input_w is None


def test_null_total_does_not_mask_live_phases():
    """Clearing the total must fall back to the phases, not to None."""
    ac = AcState()
    _feed(ac, [
        (f"{VEBUS}/Ac/ActiveIn/P", 1076),
        (f"{P}/system/0/Ac/ActiveIn/L1/Power", 1050),
        (f"{VEBUS}/Ac/ActiveIn/P", None),
    ])
    assert ac.total_input_w == 1050


def test_inverter_off_leaves_state_known_and_powers_unknown():
    """The shape the write gate has to recognise: state real, powers absent."""
    ac = AcState()
    _feed(ac, [
        (f"{P}/system/0/Ac/Consumption/L1/Power", None),
        (f"{P}/system/0/Ac/ActiveIn/L1/Power", None),
        (f"{VEBUS}/Ac/ActiveIn/P", None),
        (f"{VEBUS}/Ac/Out/L1/P", None),
        (f"{P}/system/0/Ac/In/0/Connected", None),
        (f"{VEBUS}/Ac/ActiveIn/Connected", None),
        (f"{VEBUS}/State", 0),
    ])
    assert ac.total_consumption_w is None
    assert ac.total_input_w is None
    assert ac.total_output_w is None
    assert ac.inverter_state == 0
    assert ac.inverter_state_label == "off"
    # Unknown, not False: nothing on the wire says "definitely unplugged".
    assert ac.shore_connected is None
    assert ac.is_charging_from_ac is False


# ---------------------------------------------------------------------------
# inverter DC terminals — what the LowBattery alarm actually sees
# ---------------------------------------------------------------------------

def test_inverter_dc_voltage_and_current():
    """Topic paths as captured off this Cerbo (vebus/276/Dc/0/Voltage)."""
    ac = AcState()
    assert ac.update(f"{VEBUS}/Dc/0/Voltage", 12.41) is True
    assert ac.update(f"{VEBUS}/Dc/0/Current", -146.2) is True
    assert ac.dc_voltage == 12.41
    assert ac.dc_current == -146.2


def test_inverter_dc_null_clears():
    """Switching the inverter off nulls the vebus tree; nothing may latch."""
    ac = AcState()
    _feed(ac, [(f"{VEBUS}/Dc/0/Voltage", 13.2), (f"{VEBUS}/Dc/0/Current", -16.6)])
    _feed(ac, [(f"{VEBUS}/Dc/0/Voltage", None), (f"{VEBUS}/Dc/0/Current", None)])
    assert ac.dc_voltage is None
    assert ac.dc_current is None


def test_inverter_dc_power_tracked_not_surfaced():
    """Dc/0/Power arrives under the same subscription; it is not an AC topic."""
    ac = AcState()
    assert ac.update(f"{VEBUS}/Dc/0/Power", -1900) is False
    assert ac.dc_voltage is None
