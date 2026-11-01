"""Solar charge-controller parsing and aggregation.

Topic strings and payloads are taken from a live capture off the Cerbo on
2026-08-09 (SmartSolar MPPT 100/50 at solarcharger instance 277), including
the Float state that made that day's low yield look like a fault when it was
the controller throttling into a nearly-full bank.
"""

from __future__ import annotations

from cerberus.solar import SolarState

P = "N/0123456789ab"
SC = f"{P}/solarcharger/277"


def _feed(s: SolarState, pairs) -> None:
    for topic, value in pairs:
        s.update(topic, value)


# ---------------------------------------------------------------------------
# power and yield
# ---------------------------------------------------------------------------

def test_pv_power_from_yield_topic():
    s = SolarState()
    assert s.update(f"{SC}/Yield/Power", 18.64) is True
    assert s.total_pv_power == 18.64


def test_system_pv_preferred_over_controller_sum():
    """system/0/Dc/Pv/Power covers all controllers; prefer it."""
    s = SolarState()
    _feed(s, [(f"{SC}/Yield/Power", 18.6),
              (f"{P}/system/0/Dc/Pv/Power", 33.5)])
    assert s.total_pv_power == 33.5


def test_multiple_controllers_sum():
    s = SolarState()
    _feed(s, [(f"{P}/solarcharger/277/Yield/Power", 100.0),
              (f"{P}/solarcharger/278/Yield/Power", 50.0)])
    assert s.total_pv_power == 150.0
    assert len(s.chargers) == 2


def test_charge_current_and_yield_today():
    s = SolarState()
    _feed(s, [(f"{SC}/Dc/0/Current", 1.3),
              (f"{SC}/Dc/0/Voltage", 13.39),
              (f"{SC}/History/Daily/0/Yield", 0.48),
              (f"{SC}/History/Daily/0/MaxPower", 160.0)])
    assert s.total_charge_current == 1.3
    assert s.total_yield_today_kwh == 0.48
    assert s.max_power_today == 160.0


def test_zero_watts_at_night_is_a_real_reading():
    s = SolarState()
    s.update(f"{SC}/Yield/Power", 0)
    assert s.total_pv_power == 0
    assert s.has_data is True


def test_no_data_is_none():
    s = SolarState()
    assert s.total_pv_power is None
    assert s.total_yield_today_kwh is None
    assert s.state_label is None
    assert s.has_data is False


# ---------------------------------------------------------------------------
# state — the bit that explains a low number
# ---------------------------------------------------------------------------

def test_float_state_is_throttling():
    """The 2026-08-09 case: 18W output, but State 5 means the bank is full
    and the controller is holding back, not that the array is failing."""
    s = SolarState()
    _feed(s, [(f"{SC}/State", 5), (f"{SC}/Yield/Power", 18.6)])
    assert s.state_label == "float"
    assert s.is_throttling is True


def test_bulk_state_is_not_throttling():
    """Same low wattage in Bulk means the array genuinely can't deliver."""
    s = SolarState()
    _feed(s, [(f"{SC}/State", 3), (f"{SC}/Yield/Power", 18.6)])
    assert s.state_label == "bulk"
    assert s.is_throttling is False


def test_all_state_labels_resolve():
    s = SolarState()
    for code, label in [(0, "off"), (2, "fault"), (3, "bulk"), (4, "absorption"),
                        (5, "float"), (6, "storage"), (7, "equalize"),
                        (252, "external control")]:
        s.update(f"{SC}/State", code)
        assert s.state_label == label


def test_unknown_state_labelled_not_dropped():
    s = SolarState()
    s.update(f"{SC}/State", 99)
    assert s.state_label == "unknown (99)"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def test_no_error_is_not_a_fault():
    s = SolarState()
    s.update(f"{SC}/ErrorCode", 0)
    assert s.has_error is False
    assert s.primary.error_label == "no error"


def test_bms_connection_lost_error():
    """Code 67 — actually seen on this rig on 2026-08-07."""
    s = SolarState()
    s.update(f"{SC}/ErrorCode", 67)
    assert s.has_error is True
    assert s.error_label == "BMS connection lost"


def test_unknown_error_code_still_reported():
    s = SolarState()
    s.update(f"{SC}/ErrorCode", 201)
    assert s.has_error is True
    assert s.error_label == "error 201"


# ---------------------------------------------------------------------------
# routing hygiene
# ---------------------------------------------------------------------------

def test_vebus_state_not_claimed_by_solar():
    """vebus/276/State belongs to the AC handler. Both end in /State, so the
    two parsers must not both claim it."""
    s = SolarState()
    assert s.update(f"{P}/vebus/276/State", 9) is False
    assert s.has_data is False


def test_battery_topics_rejected():
    s = SolarState()
    assert s.update(f"{P}/battery/1/Soc", 98) is False
    assert s.update(f"{P}/system/0/Dc/Battery/Power", -74) is False


def test_ac_topics_rejected():
    s = SolarState()
    assert s.update(f"{P}/system/0/Ac/Consumption/L1/Power", 143) is False


def test_malformed_topic_ignored():
    s = SolarState()
    assert s.update("garbage", 1) is False
    assert s.update("", 1) is False


def test_realistic_snapshot_2026_08_09():
    """The live reading that prompted the 'why is yield so low' question."""
    s = SolarState()
    _feed(s, [
        (f"{SC}/Connected", 1),
        (f"{SC}/State", 5),
        (f"{SC}/Mode", 1),
        (f"{SC}/ErrorCode", 0),
        (f"{SC}/Pv/V", 19.35),
        (f"{SC}/Yield/Power", 18.64),
        (f"{SC}/Dc/0/Voltage", 13.39),
        (f"{SC}/Dc/0/Current", 1.3),
        (f"{SC}/History/Daily/0/Yield", 0.48),
        (f"{SC}/History/Daily/0/MaxPower", 160.0),
        (f"{P}/system/0/Dc/Pv/Power", 18.64),
    ])
    assert s.total_pv_power == 18.64
    assert s.primary.pv_voltage == 19.35
    assert s.total_charge_current == 1.3
    assert s.total_yield_today_kwh == 0.48
    assert s.max_power_today == 160.0
    assert s.state_label == "float"
    assert s.is_throttling is True          # low yield explained, not a fault
    assert s.has_error is False
    assert s.has_data is True


# --- MPPT terminal voltage (added 2026-10-03) -------------------------------
#
# The MPPT regulates on its own Dc/0/Voltage, not on the pack. Storing it next
# to pack voltage turns the solar path's resistance into a measurement rather
# than something inferred from when the charger gave up.

def test_dc_voltage_round_trips(tmp_path):
    from cerberus.db import Db
    db = Db(str(tmp_path / "t.db"))
    db.insert_solar_sample(ts=1, pv_power=180.0, pv_voltage=18.3,
                           charge_current=13.0, yield_today_kwh=0.4,
                           state=4, error_code=0, dc_voltage=14.4)
    db.insert_solar_sample(ts=2, pv_power=0.0, pv_voltage=0.0,
                           charge_current=0.0, yield_today_kwh=0.4,
                           state=0, error_code=0)
    rows = db.conn.execute(
        "SELECT ts, dc_voltage FROM solar_samples ORDER BY ts").fetchall()
    assert rows == [(1, 14.4), (2, None)]


def test_migration_adds_dc_voltage_to_the_soak_db(tmp_path):
    """The soak DB's solar_samples predates the column; CREATE TABLE IF NOT
    EXISTS would silently leave it out."""
    import sqlite3
    from cerberus.db import Db
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE solar_samples (ts INTEGER PRIMARY KEY, "
                "pv_power REAL, pv_voltage REAL, charge_current REAL, "
                "yield_today_kwh REAL, state INTEGER, error_code INTEGER)")
    old.commit(); old.close()
    db = Db(path)
    cols = {c[1] for c in db.conn.execute("PRAGMA table_info(solar_samples)")}
    assert "dc_voltage" in cols
    assert db.insert_solar_sample(ts=1, pv_power=1.0, pv_voltage=1.0,
                                  charge_current=1.0, yield_today_kwh=0.0,
                                  state=3, error_code=0, dc_voltage=13.5)
