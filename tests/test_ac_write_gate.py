"""When does an ac_samples row get written?

Regression cover for 2026-09-17. The collector was restarted at 17:33 with the
inverter switched off, and ac_samples stopped dead — no rows for the next five
and a half hours — while battery_samples and solar_samples kept logging at full
rate. Nothing was broken: the write gate required a real *power* figure, and
with the inverter off the Cerbo publishes null for every AC power path.

Two problems hid behind that.

The rows written *before* the restart were not measurements. AcState had
retained live figures from the shore charge that ended at 02:00, so the table
reported input_w = 0.0 for fifteen hours after shore was unplugged — see
test_ac.py::test_input_total_is_cleared_by_null.

And once that was fixed, the table would have gone permanently silent while the
inverter is off, which makes "off" indistinguishable from "the collector wasn't
running". That is the same silent-gap failure mode as the sleeping MacBook and
the stale-DNS wedge, both of which looked like a healthy system from the
dashboard. So a known inverter state is now enough to write a row on its own.

Cerberus is constructed here without running __init__ — it builds an MQTT
client, a publisher, a syncer and five detectors, none of which _on_ac touches.
"""

from __future__ import annotations

import types

from cerberus.__main__ import Cerberus
from cerberus.ac import AcState

P = "N/0123456789ab"
VEBUS = f"{P}/vebus/276"


class _FakeDb:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_ac_sample(self, **kw) -> bool:
        self.rows.append(kw)
        return True


class _FakePublisher:
    def publish_ac_state(self, ac) -> None:
        pass


def _app(downsample: int = 30) -> Cerberus:
    app = object.__new__(Cerberus)
    app.ac = AcState()
    app.db = _FakeDb()
    app.publisher = _FakePublisher()
    app.last_ac_sample_ts = 0
    app.last_fast_load_ts = 0.0
    app.fast = types.SimpleNamespace(on_load=lambda w: None)
    app.last_ac_publish_ts = 0

    class _Storage:
        sample_downsample_seconds = downsample

    class _Cfg:
        storage = _Storage()

    app.cfg = _Cfg()
    return app


def test_inverter_off_still_records_a_row():
    """State is real even when every power path is null — record it."""
    app = _app()
    app._on_ac(f"{VEBUS}/State", 0)

    assert len(app.db.rows) == 1
    row = app.db.rows[0]
    assert row["inverter_state"] == 0
    assert row["consumption_w"] is None
    assert row["input_w"] is None
    assert row["output_w"] is None
    # Unknown, not False. Nothing on the wire says "definitely unplugged".
    assert row["shore_connected"] is None


def test_no_row_before_anything_real_arrives():
    """The original guard still holds: a null power alone writes nothing.

    This is the case the gate was built for — the first message of the AC tree
    arriving before its siblings, which would otherwise store a row of nulls
    that reads like a measurement of zero.
    """
    app = _app()
    app._on_ac(f"{P}/system/0/Ac/Consumption/L1/Power", None)
    assert app.db.rows == []


def test_power_figure_alone_still_records():
    """Unchanged behaviour for the normal running case."""
    app = _app()
    app._on_ac(f"{P}/system/0/Ac/Consumption/L1/Power", 143)

    assert len(app.db.rows) == 1
    assert app.db.rows[0]["consumption_w"] == 143
    assert app.db.rows[0]["inverter_state"] is None


def test_unplugging_shore_stops_reporting_input_watts():
    """End-to-end on the 09-17 sequence: charging, then unplugged.

    The second row must not inherit the first row's input figure.
    """
    app = _app(downsample=0)
    app._on_ac(f"{VEBUS}/State", 3)                 # bulk
    app._on_ac(f"{VEBUS}/Ac/ActiveIn/P", 1076)
    app._on_ac(f"{P}/system/0/Ac/ActiveIn/L1/Power", 1076)
    assert app.db.rows[-1]["input_w"] == 1076

    app._on_ac(f"{VEBUS}/Ac/ActiveIn/P", None)
    app._on_ac(f"{P}/system/0/Ac/ActiveIn/L1/Power", None)
    app._on_ac(f"{VEBUS}/State", 0)                 # off
    assert app.db.rows[-1]["input_w"] is None
    assert app.db.rows[-1]["inverter_state"] == 0


def test_downsample_still_throttles_writes():
    """The new gate must not bypass the downsample interval."""
    app = _app(downsample=30)
    for _ in range(10):
        app._on_ac(f"{VEBUS}/State", 0)
    assert len(app.db.rows) == 1


def test_inverter_dc_terminals_recorded():
    app = _app()
    app._on_ac(f"{VEBUS}/Dc/0/Voltage", 12.41)
    app._on_ac(f"{VEBUS}/Dc/0/Current", -146.2)
    app._on_ac(f"{VEBUS}/State", 9)                 # inverting

    assert len(app.db.rows) == 1
    assert app.db.rows[0]["inverter_dc_voltage"] == 12.41
    assert app.db.rows[0]["inverter_dc_current"] == -146.2


def test_inverter_dc_alone_writes_no_row():
    """A DC reading without state or power is not enough to write a row."""
    app = _app()
    app._on_ac(f"{VEBUS}/Dc/0/Voltage", 12.41)
    assert app.db.rows == []
