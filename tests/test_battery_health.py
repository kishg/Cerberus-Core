"""Soh and Capacity: cached per instance, merged by instance, never zeroed.

Added 2026-09-19 after discovering the collector had never stored either. The
trap that makes this worth testing: `system/0/Batteries` — the array the whole
pack pipeline is built on — does NOT carry them. Its elements stop at
soc/voltage/current/power/temperature/timetogo/state. Soh and Capacity exist
only on N/<portal>/battery/<inst>/{Soh,Capacity}, which nothing subscribed to,
so `parse_batteries_array` could never have populated them no matter how the
insert was written.

All three packs report Soh 100.0 / Capacity 310 today, which is exactly why
this needs to be recorded now: pack 3 carries 34.4% of discharge and takes
~9.5% more Ah per SoC point, and the BMS's own health estimate says nothing is
wrong. If that ever changes, only history will show it, and history not
recorded cannot be recovered.
"""

from __future__ import annotations

from cerberus.__main__ import Cerberus
from cerberus.db import Db
from cerberus.model import BankState, PackState


def _pack(pack_id: int, instance: int, **kw) -> PackState:
    base = dict(pack_id=pack_id, uc=f"uc{instance}", instance=instance, soc=86.0,
                voltage=13.2, current=-2.125, power=-28.0, temperature=24.0,
                timetogo=478500, ts=1789856831)
    base.update(kw)
    return PackState(**base)


def _collector() -> Cerberus:
    # Constructed without __init__ — it would build an MQTT client, publisher,
    # syncer and five detectors, none of which these two methods touch.
    c = Cerberus.__new__(Cerberus)
    c.pack_health = {}
    return c


def test_soh_and_capacity_merge_by_device_instance():
    c = _collector()
    c._on_battery_health("N/p/battery/1/Soh", 100.0)
    c._on_battery_health("N/p/battery/1/Capacity", 310)
    c._on_battery_health("N/p/battery/3/Soh", 97.5)
    bank = BankState(ts=1, packs={1: _pack(1, 1), 3: _pack(3, 3)})
    out = c._merge_health(bank)
    assert (out.packs[1].soh, out.packs[1].capacity_ah) == (100.0, 310.0)
    assert out.packs[3].soh == 97.5
    assert out.packs[3].capacity_ah is None, "partial data must not invent a capacity"


def test_instance_is_not_pack_id():
    """The topic carries the Victron device instance; pack_id comes from the uc
    token in the Batteries array. Conflating them silently attributes one
    pack's health to another."""
    c = _collector()
    c._on_battery_health("N/p/battery/7/Soh", 91.0)
    bank = BankState(ts=1, packs={1: _pack(1, 7), 2: _pack(2, 8)})
    out = c._merge_health(bank)
    assert out.packs[1].soh == 91.0      # pack 1 is on instance 7
    assert out.packs[2].soh is None


def test_unheard_pack_stays_none_not_zero():
    """'Absent is not zero' — a 0.0 SoH reads as a dead pack."""
    c = _collector()
    c._on_battery_health("N/p/battery/1/Soh", 100.0)
    out = c._merge_health(BankState(ts=1, packs={2: _pack(2, 2)}))
    assert out.packs[2].soh is None and out.packs[2].capacity_ah is None


def test_null_and_malformed_payloads_are_ignored():
    c = _collector()
    for topic, value in [
        ("N/p/battery/1/Soh", None),          # Victron publishes null when n/a
        ("N/p/battery/x/Soh", 100.0),         # non-numeric instance
        ("N/p/battery/1/Soh", "unknown"),     # non-numeric value
    ]:
        c._on_battery_health(topic, value)
    assert c.pack_health == {}


def test_merge_with_no_cached_health_is_a_passthrough():
    c = _collector()
    bank = BankState(ts=1, packs={1: _pack(1, 1)})
    assert c._merge_health(bank) is bank


def test_columns_round_trip_and_none_is_preserved(tmp_path):
    db = Db(str(tmp_path / "b.db"))
    db.insert_pack_samples([
        _pack(1, 1, soh=100.0, capacity_ah=310.0),
        _pack(2, 2),
    ])
    rows = dict(db.conn.execute(
        "select pack_id, soh from battery_samples").fetchall())
    assert rows[1] == 100.0
    assert rows[2] is None
    cap = db.conn.execute(
        "select capacity_ah from battery_samples where pack_id=1").fetchone()[0]
    assert cap == 310.0
    db.close()


def test_migration_adds_columns_to_a_pre_existing_database(tmp_path):
    """The soak DB predates these columns. CREATE TABLE IF NOT EXISTS only
    shapes a fresh file, so without an explicit ALTER this works on a dev box
    and fails on the real database — the same trap the 'source' column hit."""
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE battery_samples (ts INTEGER NOT NULL, "
                "pack_id INTEGER NOT NULL, soc REAL, voltage REAL, current REAL, "
                "power REAL, temperature REAL, timetogo INTEGER, "
                "PRIMARY KEY (ts, pack_id))")
    old.commit(); old.close()

    db = Db(path)
    cols = {c[1] for c in db.conn.execute("PRAGMA table_info(battery_samples)")}
    assert {"soh", "capacity_ah", "source"} <= cols
    assert db.insert_pack_samples([_pack(1, 1, soh=100.0, capacity_ah=310.0)]) == 1
    db.close()
