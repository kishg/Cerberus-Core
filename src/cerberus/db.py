from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, MutableMapping

from .events import Event
from .model import PackState


@dataclass(frozen=True)
class BlePackSample:
    """Latest BLE-sourced snapshot for one pack (joined cell + pack_ble row)."""
    ts: int
    pack_id: int
    pack_voltage_cv: int | None
    soc_pct: int | None
    current_a: int | None
    temp_dc: int | None
    big_counter: int | None
    field_a5: int | None
    field_a6: int | None
    flags_hex: str | None
    config_hex: str | None
    cells_mv: list[int]  # ordered by cell_idx ascending

@dataclass(frozen=True)
class CabinSample:
    """Latest reading from one cabin sensor, from whichever source wrote it."""
    ts: int
    sensor: str
    air_temp_f: float | None
    humidity_pct: float | None
    source: str


@dataclass(frozen=True)
class EngineSample:
    """Latest OBD-II-sourced engine snapshot. Every field but ts is optional:
    PIDs are polled individually and a partial answer is normal."""
    ts: int
    rpm: float | None
    coolant_c: float | None
    speed_mph: float | None
    engine_load_pct: float | None
    runtime_s: int | None


log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS battery_samples (
  ts INTEGER NOT NULL, pack_id INTEGER NOT NULL,
  soc REAL, voltage REAL, current REAL, power REAL,
  temperature REAL, timetogo INTEGER,
  soh REAL, capacity_ah REAL,
  source TEXT NOT NULL DEFAULT 'mqtt',
  PRIMARY KEY (ts, pack_id)
);
CREATE TABLE IF NOT EXISTS events (
  ts INTEGER NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_state (
  table_name TEXT PRIMARY KEY, last_synced_rowid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cell_samples (
  ts INTEGER NOT NULL, pack_id INTEGER NOT NULL, cell_idx INTEGER NOT NULL,
  voltage_mv INTEGER NOT NULL,
  PRIMARY KEY (ts, pack_id, cell_idx)
);
CREATE TABLE IF NOT EXISTS ac_samples (
  ts INTEGER PRIMARY KEY,
  consumption_w REAL, input_w REAL, output_w REAL,
  shore_connected INTEGER, inverter_state INTEGER,
  source TEXT NOT NULL DEFAULT 'mqtt',
  -- Voltage and current at the inverter's own DC terminals (vebus Dc/0).
  -- (pack voltage - inverter_dc_voltage) / |inverter_dc_current| is the
  -- inverter cable's resistance. Added 2026-10-19 after LowBattery alarms
  -- under a 1.5 kW load with the packs reading 13.0 V. Null while off.
  inverter_dc_voltage REAL, inverter_dc_current REAL
);
CREATE TABLE IF NOT EXISTS solar_samples (
  ts INTEGER PRIMARY KEY,
  pv_power REAL, pv_voltage REAL, charge_current REAL,
  yield_today_kwh REAL, state INTEGER, error_code INTEGER,
  -- Battery-side voltage at the MPPT's own output terminals (Dc/0/Voltage).
  -- The MPPT regulates on THIS, not on the pack, so (dc_voltage - pack
  -- voltage) / charge_current is the solar path's resistance. Added
  -- 2026-10-03 when it floated at 94% three days running after entering
  -- absorption with the packs at 13.37 V: inferred ~80 mOhm, against <=50
  -- just after the 09-19 rewire. This makes that a measurement.
  dc_voltage REAL,
  source TEXT NOT NULL DEFAULT 'mqtt'
);
CREATE TABLE IF NOT EXISTS cabin_samples (
  ts INTEGER NOT NULL,
  -- Which sensor. Stored rather than assumed single so a second one (the
  -- fridge compartment is the obvious next place) needs no schema change.
  sensor TEXT NOT NULL,
  -- Fahrenheit: this is ambient air, and the convention here is ambient in F,
  -- equipment in C. Same reason weather_samples.air_temp_f carries its unit.
  air_temp_f REAL,
  humidity_pct REAL,
  battery_pct INTEGER,
  rssi INTEGER,
  -- 'ble' = decoded from the sensor's own advertisement (a real 60s sample);
  -- 'govee_cloud' = whatever the sensor last uploaded over WiFi, read when out
  -- of BLE range. The cloud gives no reading timestamp. Added 2026-10-04.
  source TEXT NOT NULL DEFAULT 'ble',
  PRIMARY KEY (ts, sensor)
);
CREATE TABLE IF NOT EXISTS cerbo_wifi_samples (
  ts INTEGER PRIMARY KEY,
  -- platform/0/Network/Wifi/SignalStrength as the Cerbo publishes it (0-100;
  -- 43 on 2026-10-08, parked at home). Added because the feed drops in
  -- evening clusters while VRM stays complete: the Cerbo falls off the WiFi.
  -- No row is written while the link is down, so the gap itself is the
  -- outage; the rows before it show whether the signal sagged first.
  signal_strength INTEGER,
  -- platform/0/Network/Wifi/State ('ready', 'online', ...).
  state TEXT
);
CREATE TABLE IF NOT EXISTS temperature_samples (
  ts INTEGER NOT NULL,
  -- Victron device instance. Instance 24 is the only one on this rig and is
  -- NOT an air sensor: it tracks charge current (67 C at 56 A) and marks an
  -- unidentified hot joint in the charge path. Keyed by instance rather than
  -- assumed single, because a Bluetooth sensor paired to the Cerbo would
  -- arrive here as a second one.
  instance INTEGER NOT NULL,
  -- Celsius. MQTT publishes C while VRM renders the same sensor in F, which
  -- is exactly the confusion the unit-suffixed name exists to prevent.
  temperature_c REAL,
  -- The raw analog reading behind it (3.018 when temperature_c was 28.68).
  -- Stored because it is the only clue to what kind of probe this is, and
  -- that question is still open.
  raw_value REAL,
  PRIMARY KEY (ts, instance)
);
-- One row every few minutes while the collector runs (added 2026-10-31).
-- A gap in every table that still has these rows was Cerberus running but
-- hearing nothing; a gap without them, Cerberus not running at all (asleep,
-- off). Read by cerberus.health.
CREATE TABLE IF NOT EXISTS collector_uptime (
  ts INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS weather_samples (
  ts INTEGER PRIMARY KEY,
  -- Fahrenheit, not Celsius: project convention is ambient in F, equipment in
  -- C, and the unit lives in the column name so a join can never silently mix
  -- this with battery_samples.temperature (which is C).
  air_temp_f REAL,
  cloud_pct REAL, humidity_pct REAL, wind_mph REAL,
  source TEXT NOT NULL DEFAULT 'metno',
  -- Where the forecast was fetched for, and how that was known: 'gps' (live
  -- fix), 'phone' (the owner's phone, with the van), 'gps_last' /
  -- 'phone_last' (last fix on record) or 'config'. Added 2026-10-31;
  -- earlier rows are null and were all fetched for the configured position.
  lat REAL, lon REAL, position_source TEXT
);
CREATE TABLE IF NOT EXISTS pack_ble_samples (
  ts INTEGER NOT NULL, pack_id INTEGER NOT NULL,
  pack_voltage_cv INTEGER, soc_pct INTEGER, current_a INTEGER,
  temp_dc INTEGER, big_counter INTEGER,
  field_a5 INTEGER, field_a6 INTEGER,
  flags_hex TEXT, config_hex TEXT,
  PRIMARY KEY (ts, pack_id)
);
CREATE TABLE IF NOT EXISTS acr_samples (
  ts INTEGER PRIMARY KEY,
  -- The Lithionics ACR's unprompted ~1/s stream, e.g.
  -- "*,0,0,1261,1325,0,0,0,1,6002". Only fields 3, 4, 8 and 9 are mapped
  -- (2026-09-24, parked, contactor open); 1, 2 and 5-7 read zero there and
  -- are stored under positional names until a drive capture maps them.
  -- Contactor state and current are the likely candidates. The ACR is the
  -- only window onto the alternator circuit: the Cerbo cannot see it at all.
  field1 INTEGER, field2 INTEGER,
  -- Chassis/start battery volts in hundredths. Field 3. No other source on
  -- this rig measures the chassis battery.
  chassis_v_cv INTEGER,
  -- House bus volts in hundredths, as the ACR sees it at the contactor —
  -- reads high under charge because of cable drop. Field 4.
  house_v_cv INTEGER,
  field5 INTEGER, field6 INTEGER, field7 INTEGER,
  chassis_present INTEGER,  -- field 8, "Chassis Present" in $LIST
  version INTEGER,          -- field 9, "Version=6002" in $LIST
  -- The line as received. Kept because the mapping is incomplete and a drive
  -- may show a frame shape the parser does not expect.
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acr_settings (
  ts INTEGER PRIMARY KEY,
  -- The ACR's own settings, read back with `$LIST` over the connection the
  -- sidecar already holds (daily and on each connect). Every read is kept, so
  -- the table also answers "were they still set on day X", e.g. after the
  -- ACR loses power. Read-only: no write command is ever sent.
  firmware TEXT,           -- header line, e.g. "Lithionics ACR v6.0.05"
  -- {"House Full Voltage": 1420, ...}: values in the ACR's own units
  -- (hundredths of a volt for voltages), as numbers where they parse.
  settings TEXT NOT NULL,
  raw TEXT NOT NULL        -- the reply lines as received, newline-joined
);
CREATE TABLE IF NOT EXISTS engine_samples (
  ts INTEGER PRIMARY KEY,
  -- Engine RPM, the reason this table exists. Alternator current cannot
  -- distinguish "engine running hard" from "engine running at all", so it
  -- could not settle why 2026-09-24/26 overshot to 14.75/14.65 V while 09-28
  -- stayed at 14.40 V at a higher +234.8 A. Join against battery_samples on
  -- ts to put a tachometer trace beside the charge trace.
  rpm REAL,
  -- Celsius: equipment, not ambient air. Same convention as
  -- battery_samples.temperature and temperature_samples.temperature_c.
  coolant_c REAL,
  -- mph, converted from the PID's km/h, because every distance in the
  -- project notes is imperial. Unit in the name for the same reason
  -- weather_samples.air_temp_f carries its own.
  speed_mph REAL,
  engine_load_pct REAL,
  -- Seconds since engine start, straight off PID 011F. 16-bit, so it wraps
  -- at 65535 (~18 h) — do not treat it as a monotonic odometer.
  runtime_s INTEGER
);
-- Everything the OBD reader polls, one row per signal per read. Long rather
-- than wide because the set of signals is discovered per vehicle (supported-
-- PID bitmaps) and grows by config (obd.extra), and a schema migration per
-- reverse-engineered Mercedes value would be absurd. engine_samples stays as
-- the narrow, fast-to-query view of the five core PIDs.
--
-- raw is stored for every row, decoded or not. Formulas for reverse-engineered
-- values WILL be wrong at first; with the raw reply kept, history is re-decoded
-- rather than lost.
CREATE TABLE IF NOT EXISTS obd_samples (
  ts INTEGER NOT NULL,
  -- Request CAN ID: 7DF for standard broadcast PIDs, the module's own ID for
  -- a manufacturer read. Part of the key because two modules can expose
  -- signals under the same name.
  ecu TEXT NOT NULL,
  name TEXT NOT NULL,
  raw TEXT,
  value REAL,
  PRIMARY KEY (ts, ecu, name)
);
-- What each obd_samples.name meant when it was logged: request bytes, unit and
-- the formula in force. Upserted each session, so the formula used to decode
-- old rows can be recovered by comparing against config history.
CREATE TABLE IF NOT EXISTS obd_signals (
  ecu TEXT NOT NULL, name TEXT NOT NULL,
  request TEXT NOT NULL, unit TEXT, formula TEXT,
  first_seen INTEGER, last_seen INTEGER,
  PRIMARY KEY (ecu, name)
);
-- Vehicle values imported from another system through the HA broker
-- (ha_mqtt.import_topics), e.g. the chassis's own cloud data via Home
-- Assistant. One row per value per snapshot; source names the system.
CREATE TABLE IF NOT EXISTS vehicle_samples (
  ts INTEGER NOT NULL, source TEXT NOT NULL, name TEXT NOT NULL, value REAL,
  PRIMARY KEY (ts, source, name)
);
-- Full-rate Dc/0 voltage and current around alarms and heavy loads
-- (cerberus.fastcapture). series is e.g. vebus/276/Voltage, battery/1/Current.
CREATE TABLE IF NOT EXISTS fast_samples (
  ts REAL NOT NULL, series TEXT NOT NULL, value REAL NOT NULL,
  PRIMARY KEY (ts, series)
);
CREATE TABLE IF NOT EXISTS fast_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts REAL NOT NULL, end_ts REAL, reason TEXT NOT NULL
);
-- Trouble codes read once per engine-on session. Read-only (services 03/07);
-- nothing here ever clears a code. codes is comma-separated, '' = none.
CREATE TABLE IF NOT EXISTS obd_dtcs (
  ts INTEGER NOT NULL, service TEXT NOT NULL, codes TEXT, raw TEXT,
  PRIMARY KEY (ts, service)
);
-- One row per drive, written after the engine has been off long enough to
-- call it over. source says how the boundaries were found: 'obd' from RPM,
-- 'electrical' from alternator current (all history before the adapter, and
-- any drive the laptop missed the OBD side of). metrics is JSON so a new
-- per-drive figure does not need a migration; see cerberus.drives.
-- One row per local calendar day the MPPT ran: solar path resistance and
-- when/at what SoC it floated. See cerberus.solar_path.
CREATE TABLE IF NOT EXISTS solar_days (
  day TEXT PRIMARY KEY,
  metrics TEXT NOT NULL,
  flags TEXT,
  created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drives (
  start_ts INTEGER PRIMARY KEY,
  end_ts INTEGER NOT NULL,
  source TEXT NOT NULL,
  metrics TEXT NOT NULL,
  flags TEXT,
  report_path TEXT,
  created_ts INTEGER NOT NULL
);

-- Every occurrence of a finding, numbered for good (cerberus.findings_log).
-- Written by the collector; the owner's resolution notes, which close an
-- occurrence, live in the notes file beside config.yaml (cerberus.notes).
CREATE TABLE IF NOT EXISTS finding_log (
  num INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL,      -- the highest it reached
  opened_ts INTEGER NOT NULL,
  cleared_ts INTEGER,          -- when the data stopped showing it; NULL while open
  last_seen INTEGER,
  explain TEXT,                -- as it last read
  prev_num INTEGER             -- the previous occurrence of the same finding
);
"""


class Db:
    # Two processes write this database: the MQTT collector and the BLE
    # sidecar (`python -m cerberus.ble`), both pointed at the same file. WAL
    # keeps readers from blocking, but it does not serialise *writers* — and
    # SQLite's default busy timeout is 0, so a collision raises
    # ``database is locked`` immediately and that sample is simply lost.
    # Writes are sub-millisecond bursts every 30s, so a collision is unlikely
    # per event but accumulates over a long run. Five seconds is far longer
    # than any write here needs and costs nothing when there is no contention.
    BUSY_TIMEOUT_MS = 5000

    #
    # One connection PER THREAD, for the same reason within this process. The
    # collector touches the database from the paho callback thread (samples,
    # detectors) and from the main loop (weather, Govee cloud, BLE/OBD
    # publish, drives). A single shared connection with check_same_thread=False
    # let those interleave mid-statement: on 2026-10-04 18:18:23 a detector
    # read on the paho thread failed with ``InterfaceError: bad parameter or
    # other API misuse`` in the same second the main loop wrote a cabin row.
    # Separate connections are exactly the case WAL + busy_timeout already
    # handle across the two processes, so `conn` is a per-thread property and
    # every existing ``db.conn.execute(...)`` call keeps working unchanged.

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        c = getattr(self._local, "conn", None)
        if c is None:
            # check_same_thread=False only so close() can close every thread's
            # connection from the shutting-down thread; each one is still used
            # by exactly one thread.
            c = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            c.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    def _migrate(self) -> None:
        """Additive column migrations for databases created before a column existed.

        SCHEMA uses CREATE TABLE IF NOT EXISTS, which only ever shapes a *fresh*
        file — an existing database keeps whatever columns it was built with and
        silently ignores the new definition. Every column added after first
        release therefore needs an explicit ALTER here as well as an entry in
        SCHEMA, or the collector works on a dev box and fails on the soak DB.

        ALTER TABLE ADD COLUMN is O(1) in SQLite and safe to run against a
        database another process has open, so this is unconditional on startup
        rather than gated behind a version counter.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(battery_samples)")}
        if "source" not in cols:
            # 'mqtt' = collected live off the Cerbo, the only provenance that
            # existed before backfill. Rows imported from VRM carry 'vrm' and
            # are 15-minute bucket averages, not 30s samples — see
            # tools/vrm_backfill.py.
            self.conn.execute(
                "ALTER TABLE battery_samples "
                "ADD COLUMN source TEXT NOT NULL DEFAULT 'mqtt'"
            )
        # Added 2026-09-19. All three packs report Soh 100.0 / Capacity 310 Ah
        # today, so there is nothing to see yet — that is exactly the point.
        # Divergence is only legible against history, and history that was
        # never recorded cannot be recovered after the fact.
        if "soh" not in cols:
            self.conn.execute("ALTER TABLE battery_samples ADD COLUMN soh REAL")
        if "capacity_ah" not in cols:
            self.conn.execute("ALTER TABLE battery_samples ADD COLUMN capacity_ah REAL")
        solar_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(solar_samples)")}
        if "dc_voltage" not in solar_cols:
            self.conn.execute("ALTER TABLE solar_samples ADD COLUMN dc_voltage REAL")
        # Same provenance column as battery_samples, added 2026-10-04 so VRM
        # backfill can fill solar/AC gaps without being mistaken for live
        # 30s samples. The collector's INSERTs name their columns and never
        # set it, so live rows keep the 'mqtt' default.
        if "source" not in solar_cols:
            self.conn.execute(
                "ALTER TABLE solar_samples ADD COLUMN source TEXT NOT NULL DEFAULT 'mqtt'"
            )
        cabin_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(cabin_samples)")}
        if "source" not in cabin_cols:
            self.conn.execute(
                "ALTER TABLE cabin_samples ADD COLUMN source TEXT NOT NULL DEFAULT 'ble'"
            )
        ac_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ac_samples)")}
        if "source" not in ac_cols:
            self.conn.execute(
                "ALTER TABLE ac_samples ADD COLUMN source TEXT NOT NULL DEFAULT 'mqtt'"
            )
        if "inverter_dc_voltage" not in ac_cols:
            self.conn.execute("ALTER TABLE ac_samples ADD COLUMN inverter_dc_voltage REAL")
        if "inverter_dc_current" not in ac_cols:
            self.conn.execute("ALTER TABLE ac_samples ADD COLUMN inverter_dc_current REAL")
        wx_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(weather_samples)")}
        for col, kind in (("lat", "REAL"), ("lon", "REAL"), ("position_source", "TEXT")):
            if col not in wx_cols:
                self.conn.execute(f"ALTER TABLE weather_samples ADD COLUMN {col} {kind}")

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        # A thread that touches the Db after close gets a fresh connection
        # rather than a closed one.
        self._local = threading.local()

    def insert_pack_samples(self, packs: Iterable[PackState]) -> int:
        rows = [
            (p.ts, p.pack_id, p.soc, p.voltage, p.current, p.power, p.temperature,
             p.timetogo, p.soh, p.capacity_ah)
            for p in packs
        ]
        if not rows:
            return 0
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO battery_samples "
                "(ts, pack_id, soc, voltage, current, power, temperature, timetogo, "
                "soh, capacity_ah) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)
        except sqlite3.Error as e:
            log.error("battery_samples insert failed: %s", e)
            return 0

    def insert_ac_sample(
        self,
        ts: int,
        consumption_w: float | None,
        input_w: float | None,
        output_w: float | None,
        shore_connected: bool | None,
        inverter_state: int | None,
        inverter_dc_voltage: float | None = None,
        inverter_dc_current: float | None = None,
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO ac_samples "
                "(ts, consumption_w, input_w, output_w, shore_connected, inverter_state, "
                "inverter_dc_voltage, inverter_dc_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, consumption_w, input_w, output_w,
                    None if shore_connected is None else int(shore_connected),
                    inverter_state, inverter_dc_voltage, inverter_dc_current,
                ),
            )
            return True
        except sqlite3.Error as e:
            log.error("ac_samples insert failed: %s", e)
            return False

    def insert_solar_sample(
        self,
        ts: int,
        pv_power: float | None,
        pv_voltage: float | None,
        charge_current: float | None,
        yield_today_kwh: float | None,
        state: int | None,
        error_code: int | None,
        dc_voltage: float | None = None,
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO solar_samples "
                "(ts, pv_power, pv_voltage, charge_current, yield_today_kwh, state, "
                "error_code, dc_voltage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, pv_power, pv_voltage, charge_current, yield_today_kwh, state,
                 error_code, dc_voltage),
            )
            return True
        except sqlite3.Error as e:
            log.error("solar_samples insert failed: %s", e)
            return False

    def insert_cabin_sample(
        self,
        ts: int,
        sensor: str,
        air_temp_f: float | None,
        humidity_pct: float | None,
        battery_pct: int | None,
        rssi: int | None = None,
        source: str = "ble",
    ) -> bool:
        # BLE is the primary source: on a same-second (ts, sensor) clash a BLE
        # row replaces a cloud one, but a cloud row never replaces a BLE one.
        verb = "INSERT OR REPLACE" if source == "ble" else "INSERT OR IGNORE"
        try:
            self.conn.execute(
                f"{verb} INTO cabin_samples "
                "(ts, sensor, air_temp_f, humidity_pct, battery_pct, rssi, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, sensor, air_temp_f, humidity_pct, battery_pct, rssi, source),
            )
            return True
        except sqlite3.Error as e:
            log.error("cabin_samples insert failed: %s", e)
            return False

    def insert_cerbo_wifi_sample(
        self, ts: int, signal_strength: int | None, state: str | None
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO cerbo_wifi_samples (ts, signal_strength, state) "
                "VALUES (?, ?, ?)",
                (ts, signal_strength, state),
            )
            return True
        except sqlite3.Error as e:
            log.error("cerbo_wifi_samples insert failed: %s", e)
            return False

    def insert_temperature_sample(
        self,
        ts: int,
        instance: int,
        temperature_c: float | None,
        raw_value: float | None,
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO temperature_samples "
                "(ts, instance, temperature_c, raw_value) VALUES (?, ?, ?, ?)",
                (ts, instance, temperature_c, raw_value),
            )
            return True
        except sqlite3.Error as e:
            log.error("temperature_samples insert failed: %s", e)
            return False

    def insert_weather_sample(
        self,
        ts: int,
        air_temp_f: float | None,
        cloud_pct: float | None,
        humidity_pct: float | None,
        wind_mph: float | None,
        source: str = "metno",
        lat: float | None = None,
        lon: float | None = None,
        position_source: str | None = None,
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO weather_samples "
                "(ts, air_temp_f, cloud_pct, humidity_pct, wind_mph, source, "
                "lat, lon, position_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, air_temp_f, cloud_pct, humidity_pct, wind_mph, source,
                 lat, lon, position_source),
            )
            return True
        except sqlite3.Error as e:
            log.error("weather_samples insert failed: %s", e)
            return False

    def insert_engine_sample(
        self,
        ts: int,
        rpm: float | None,
        coolant_c: float | None,
        speed_mph: float | None,
        engine_load_pct: float | None,
        runtime_s: int | None,
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO engine_samples "
                "(ts, rpm, coolant_c, speed_mph, engine_load_pct, runtime_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, rpm, coolant_c, speed_mph, engine_load_pct, runtime_s),
            )
            return True
        except sqlite3.Error as e:
            log.error("engine_samples insert failed: %s", e)
            return False

    def latest_engine_sample(self, max_age_seconds: int | None = None
                             ) -> EngineSample | None:
        """Most recent engine row, or None if there is none fresh enough.

        Mirrors latest_ble_per_pack: the OBD reader lives in the sidecar and
        does not speak MQTT, so the collector reads its output back out of
        SQLite to publish it. The freshness cutoff is what keeps a retained HA
        entity from showing last week's RPM — out of BLE range, or with the
        ignition off, this table simply stops.
        """
        cutoff_clause = ""
        params: tuple = ()
        if max_age_seconds is not None:
            cutoff_clause = "WHERE ts >= strftime('%s','now') - ?"
            params = (int(max_age_seconds),)
        row = self.conn.execute(
            "SELECT ts, rpm, coolant_c, speed_mph, engine_load_pct, runtime_s "
            f"FROM engine_samples {cutoff_clause} ORDER BY ts DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        return EngineSample(
            ts=int(row[0]),
            rpm=row[1],
            coolant_c=row[2],
            speed_mph=row[3],
            engine_load_pct=row[4],
            runtime_s=row[5],
        )

    def insert_obd_samples(self, ts: int,
                           rows: Iterable[tuple[str, str, str | None, float | None]]
                           ) -> int:
        """rows: (ecu, name, raw, value). One transaction per poll cycle."""
        batch = [(ts, ecu, name, raw, value) for ecu, name, raw, value in rows]
        if not batch:
            return 0
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO obd_samples (ts, ecu, name, raw, value) "
                "VALUES (?, ?, ?, ?, ?)",
                batch,
            )
            return len(batch)
        except sqlite3.Error as e:
            log.error("obd_samples insert failed: %s", e)
            return 0

    def upsert_obd_signals(self, ts: int,
                           specs: Iterable[tuple[str, str, str, str, str]]) -> None:
        """specs: (ecu, name, request, unit, formula)."""
        try:
            self.conn.executemany(
                "INSERT INTO obd_signals "
                "(ecu, name, request, unit, formula, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ecu, name) DO UPDATE SET request = excluded.request, "
                "unit = excluded.unit, formula = excluded.formula, "
                "last_seen = excluded.last_seen",
                [(e, n, r, u, f, ts, ts) for e, n, r, u, f in specs],
            )
        except sqlite3.Error as e:
            log.error("obd_signals upsert failed: %s", e)

    def insert_vehicle_samples(self, ts: int, source: str,
                               values: dict[str, float]) -> int:
        rows = [(ts, source, k, float(v)) for k, v in values.items()]
        if not rows:
            return 0
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO vehicle_samples (ts, source, name, value) "
                "VALUES (?, ?, ?, ?)", rows)
            return len(rows)
        except sqlite3.Error as e:
            log.error("vehicle_samples insert failed: %s", e)
            return 0

    def insert_fast_samples(self, rows: list[tuple[float, str, float]]) -> None:
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO fast_samples (ts, series, value) VALUES (?, ?, ?)", rows)
        except sqlite3.Error as e:
            log.error("fast_samples insert failed: %s", e)

    def open_fast_window(self, start_ts: float, reason: str) -> int | None:
        try:
            cur = self.conn.execute(
                "INSERT INTO fast_windows (start_ts, reason) VALUES (?, ?)", (start_ts, reason))
            return cur.lastrowid
        except sqlite3.Error as e:
            log.error("fast_windows insert failed: %s", e)
            return None

    def update_fast_window(self, window_id: int, end_ts: float | None, reason: str) -> None:
        try:
            self.conn.execute("UPDATE fast_windows SET end_ts = COALESCE(?, end_ts), reason = ? "
                              "WHERE id = ?", (end_ts, reason, window_id))
        except sqlite3.Error as e:
            log.error("fast_windows update failed: %s", e)

    def prune_fast_samples(self, before_ts: float) -> None:
        try:
            self.conn.execute("DELETE FROM fast_samples WHERE ts < ?", (before_ts,))
            self.conn.execute("DELETE FROM fast_windows WHERE start_ts < ?", (before_ts,))
        except sqlite3.Error as e:
            log.error("fast capture prune failed: %s", e)

    def insert_dtcs(self, ts: int, service: str, codes: list[str] | None,
                    raw: str | None) -> None:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO obd_dtcs (ts, service, codes, raw) "
                "VALUES (?, ?, ?, ?)",
                (ts, service, None if codes is None else ",".join(codes), raw),
            )
        except sqlite3.Error as e:
            log.error("obd_dtcs insert failed: %s", e)

    def get_kv(self, key: str) -> str | None:
        cur = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        except sqlite3.Error as e:
            log.error("kv set failed: %s", e)

    def kv_mapping(self) -> "MutableMapping[str, str]":
        """A dict-like view of the kv table for detector state persistence."""
        return _DbKv(self)

    def insert_cell_samples(self, ts: int, pack_id: int,
                            cell_voltages_mv: list[int]) -> int:
        rows = [(ts, pack_id, idx, mv)
                for idx, mv in enumerate(cell_voltages_mv, start=1)]
        if not rows:
            return 0
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO cell_samples "
                "(ts, pack_id, cell_idx, voltage_mv) VALUES (?, ?, ?, ?)",
                rows,
            )
            return len(rows)
        except sqlite3.Error as e:
            log.error("cell_samples insert failed: %s", e)
            return 0

    def insert_pack_ble_sample(self, **fields) -> int:
        cols = ("ts", "pack_id", "pack_voltage_cv", "soc_pct", "current_a",
                "temp_dc", "big_counter", "field_a5", "field_a6",
                "flags_hex", "config_hex")
        try:
            self.conn.execute(
                f"INSERT OR REPLACE INTO pack_ble_samples ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                tuple(fields.get(c) for c in cols),
            )
            return 1
        except sqlite3.Error as e:
            log.error("pack_ble_samples insert failed: %s", e)
            return 0

    def insert_acr_sample(self, **fields) -> int:
        cols = ("ts", "field1", "field2", "chassis_v_cv", "house_v_cv",
                "field5", "field6", "field7", "chassis_present", "version", "raw")
        try:
            self.conn.execute(
                f"INSERT OR REPLACE INTO acr_samples ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                tuple(fields.get(c) for c in cols),
            )
            return 1
        except sqlite3.Error as e:
            log.error("acr_samples insert failed: %s", e)
            return 0

    def insert_acr_settings(self, ts: int, firmware: str | None,
                            settings: dict, raw: str) -> int:
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO acr_settings (ts, firmware, settings, raw) "
                "VALUES (?, ?, ?, ?)",
                (ts, firmware, json.dumps(settings, sort_keys=True), raw),
            )
            return 1
        except sqlite3.Error as e:
            log.error("acr_settings insert failed: %s", e)
            return 0

    def latest_acr_settings(self) -> tuple[int, str | None, dict] | None:
        """(ts, firmware, settings) of the last `$LIST` read, or None."""
        row = self.conn.execute(
            "SELECT ts, firmware, settings FROM acr_settings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            settings = json.loads(row[2])
        except ValueError:
            settings = {}
        return row[0], row[1], settings if isinstance(settings, dict) else {}

    def latest_cabin_per_sensor(self, max_age_seconds: int | None = None
                                ) -> dict[str, CabinSample]:
        """Most recent cabin row per sensor, BLE or cloud alike.

        The BLE sidecar and the Govee cloud poller both write cabin_samples and
        neither speaks to HA, so the collector reads the newest row back out
        to publish it — same split as latest_engine_sample.
        """
        cutoff_clause = ""
        params: tuple = ()
        if max_age_seconds is not None:
            cutoff_clause = "WHERE ts >= strftime('%s','now') - ?"
            params = (int(max_age_seconds),)
        rows = self.conn.execute(
            "SELECT c.ts, c.sensor, c.air_temp_f, c.humidity_pct, c.source "
            "FROM cabin_samples c JOIN ("
            f"  SELECT sensor, MAX(ts) AS ts FROM cabin_samples {cutoff_clause} "
            "  GROUP BY sensor"
            ") m ON c.sensor = m.sensor AND c.ts = m.ts",
            params,
        ).fetchall()
        return {
            r[1]: CabinSample(ts=int(r[0]), sensor=r[1], air_temp_f=r[2],
                              humidity_pct=r[3], source=r[4])
            for r in rows
        }

    def latest_ble_per_pack(self, max_age_seconds: int | None = None
                            ) -> dict[int, BlePackSample]:
        """Return the most recent BLE sample per pack, joined with its cells.
        If `max_age_seconds` is given, exclude samples older than that."""
        cutoff_clause = ""
        params: tuple = ()
        if max_age_seconds is not None:
            cutoff_clause = "WHERE ts >= strftime('%s','now') - ?"
            params = (int(max_age_seconds),)
        rows = self.conn.execute(
            f"SELECT pack_id, MAX(ts) FROM pack_ble_samples {cutoff_clause} GROUP BY pack_id",
            params,
        ).fetchall()
        out: dict[int, BlePackSample] = {}
        for pack_id, ts in rows:
            row = self.conn.execute(
                "SELECT ts, pack_id, pack_voltage_cv, soc_pct, current_a, temp_dc, "
                "big_counter, field_a5, field_a6, flags_hex, config_hex "
                "FROM pack_ble_samples WHERE pack_id = ? AND ts = ?",
                (pack_id, ts),
            ).fetchone()
            if not row:
                continue
            cells = [
                mv for (mv,) in self.conn.execute(
                    "SELECT voltage_mv FROM cell_samples "
                    "WHERE pack_id = ? AND ts = ? ORDER BY cell_idx",
                    (pack_id, ts),
                )
            ]
            out[pack_id] = BlePackSample(
                ts=row[0], pack_id=row[1],
                pack_voltage_cv=row[2], soc_pct=row[3], current_a=row[4],
                temp_dc=row[5], big_counter=row[6],
                field_a5=row[7], field_a6=row[8],
                flags_hex=row[9], config_hex=row[10],
                cells_mv=cells,
            )
        return out

    def mark_uptime(self, ts: int) -> None:
        try:
            self.conn.execute("INSERT OR IGNORE INTO collector_uptime (ts) VALUES (?)", (ts,))
        except sqlite3.Error as e:
            log.error("collector_uptime insert failed: %s", e)

    def insert_event(self, ev: Event) -> None:
        try:
            self.conn.execute(
                "INSERT INTO events(ts, type, severity, detail) VALUES(?, ?, ?, ?)",
                (ev.ts, ev.type, ev.severity.value, ev.to_json()),
            )
        except sqlite3.Error as e:
            log.error("event insert failed: %s", e)

    # ---- sync-state helpers (M3) ------------------------------------------

    _SYNC_TABLES = ("battery_samples", "events")

    def get_last_synced(self, table: str) -> int:
        if table not in self._SYNC_TABLES:
            raise ValueError(f"unsupported sync table: {table}")
        row = self.conn.execute(
            "SELECT last_synced_rowid FROM sync_state WHERE table_name = ?", (table,)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_last_synced(self, table: str, rowid: int) -> None:
        if table not in self._SYNC_TABLES:
            raise ValueError(f"unsupported sync table: {table}")
        try:
            self.conn.execute(
                "INSERT INTO sync_state(table_name, last_synced_rowid) VALUES(?, ?) "
                "ON CONFLICT(table_name) DO UPDATE SET last_synced_rowid = excluded.last_synced_rowid",
                (table, int(rowid)),
            )
        except sqlite3.Error as e:
            log.error("sync_state set failed for %s: %s", table, e)

    def fetch_since(self, table: str, last_rowid: int, limit: int) -> list[tuple[int, dict]]:
        """Return up to `limit` rows from `table` with rowid > last_rowid, ordered
        by rowid. Each element is (rowid, dict). The dict shape matches the
        column names of the table — used directly as the sync payload."""
        if table not in self._SYNC_TABLES:
            raise ValueError(f"unsupported sync table: {table}")
        cur = self.conn.execute(
            f"SELECT rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, limit),
        )
        cols = [c[0] for c in cur.description]
        out: list[tuple[int, dict]] = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            rid = int(d.pop("rowid"))
            out.append((rid, d))
        return out


class _DbKv:
    """Minimal MutableMapping shim over Db.get_kv / set_kv for detectors."""

    def __init__(self, db: Db):
        self._db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        v = self._db.get_kv(key)
        return v if v is not None else default

    def __getitem__(self, key: str) -> str:
        v = self._db.get_kv(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key: str, value: str) -> None:
        self._db.set_kv(key, value)

    def __contains__(self, key: str) -> bool:
        return self._db.get_kv(key) is not None

    def __delitem__(self, key: str) -> None:
        try:
            self._db.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        except sqlite3.Error as e:
            log.error("kv delete failed: %s", e)

    def __iter__(self):
        cur = self._db.conn.execute("SELECT key FROM kv")
        for (k,) in cur:
            yield k

    def __len__(self) -> int:
        cur = self._db.conn.execute("SELECT COUNT(*) FROM kv")
        return cur.fetchone()[0]
