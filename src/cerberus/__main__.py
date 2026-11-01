from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid

import yaml
import time
from dataclasses import replace
from pathlib import Path

from . import config as cfg_mod
from . import detect as detect_mod
from . import detectors as detectors_mod
from . import envfile
from . import phonefix
from . import drives as drives_mod
from . import heater as heater_mod
from . import live as live_mod
from .ha_mqtt import HaBroker, NoBroker
from . import vehicle as vehicle_mod
from . import vrm_cloud
from . import solar_path as solar_path_mod
from .ac import AcState
from .alarms import AlarmState, parse_alarm_topic
from .db import Db
from .events import Severity, make as make_event
from .location import VanLocation
from .log import setup_logging
from .model import SOURCE_MONITOR, BankState, parse_batteries_array, parse_battery_monitor
from .mqtt_client import CerboMqtt
from .publisher import Publisher
from .solar import SolarState
from .syncer import Syncer
from .govee_cloud import fetch as fetch_govee_cloud
from .weather import fetch as fetch_weather

log = logging.getLogger("cerberus")

HEARTBEAT_STALE_SECONDS = 60
LIVENESS_CHECK_PERIOD = 5
BLE_TICK_PERIOD = 15           # publish BLE-derived stats this often
BLE_FRESHNESS_SECONDS = 120    # ignore BLE samples older than this
OBD_TICK_PERIOD = 5            # engine RPM moves fast; republish it often
# Tighter than BLE_FRESHNESS_SECONDS on purpose. The sidecar polls every 2 s
# while the engine runs and stops writing entirely the moment the ignition goes
# off, so anything older than this means "not running" — and a stale RPM is the
# one reading here that must never linger.
OBD_FRESHNESS_SECONDS = 30
CABIN_TICK_PERIOD = 60         # BLE samples once a minute; nothing to gain faster
# Twice the cloud poll interval (5 min) plus slack, so one failed cloud fetch
# while out of BLE range does not flap the HA entity unavailable.
CABIN_FRESHNESS_SECONDS = 900
AC_PUBLISH_PERIOD = 5          # AC topics arrive at wire rate; publish this often
SOLAR_PUBLISH_PERIOD = 5       # same for the solarcharger tree
ARRAY_VOC_MARGIN = 1.25        # PV above array_voc_v by this much = misconfigured
UPTIME_PERIOD = 300            # collector_uptime cadence; health reads it
KV_DEVICE_ID = "device.id"     # stable box identity (pairing, sync, relay later)
# A drive is only processed SETTLE_S (15 min) after it ends, so checking more
# often than this buys nothing; each check is a few indexed queries.
DRIVES_TICK_PERIOD = 300
CLOUD_RETRY_SECONDS = 300       # after a failed cloud start (no internet, VRM down)
HEATER_TICK_PERIOD = 30         # one ac_samples write; on/off within a sample


def should_force_reconnect(
    cerbo_online: bool,
    ever_saw_heartbeat: bool,
    mqtt_connected: bool,
) -> bool:
    """Whether a stalled data feed warrants rebuilding the MQTT session.

    The target is the half-open connection: paho still reports connected, so
    nothing in mqtt_client can notice, but the GX stopped publishing because
    its keepalive expired. Only the data feed reveals it.

    Requires ever_saw_heartbeat so startup doesn't immediately force a
    reconnect — before the first heartbeat arrives the Cerbo reads as offline
    purely because we haven't heard from it yet, which is not a stall.
    """
    return (not cerbo_online) and ever_saw_heartbeat and mqtt_connected


class Cerberus:
    def __init__(self, cfg: cfg_mod.Config) -> None:
        self.cfg = cfg
        self.db = Db(cfg.storage.sqlite_path)
        self.detectors = detectors_mod.from_config(
            cfg.thresholds, cfg.full_charge, self.db.kv_mapping()
        )
        self.cell_detector = detectors_mod.CellImbalanceDetector(
            in_pack_threshold_mv=float(cfg.thresholds.get("cell_imbalance_in_pack_mv", 50)),
            cross_pack_threshold_mv=float(cfg.thresholds.get("cell_imbalance_cross_pack_mv", 100)),
            sustain_seconds=int(cfg.thresholds.get("cell_imbalance_sustain_seconds", 60)),
        )
        self.solar_open_detector = detectors_mod.SolarOutputOpenDetector(
            pv_voltage_min=float(cfg.thresholds.get("solar_open_pv_voltage_min", 15.0)),
            sustain_seconds=int(cfg.thresholds.get("solar_open_sustain_seconds", 90)),
            full_soc_guard_pct=float(cfg.thresholds.get("solar_open_full_soc_pct", 97)),
            charging_guard_a=float(cfg.thresholds.get("solar_open_charging_guard_a", 20.0)),
            open_circuit_voltage=float(cfg.thresholds.get("solar_open_circuit_voltage", 18.5)),
            recent_power_w=float(cfg.thresholds.get("solar_open_recent_power_w", 25.0)),
            recent_window_seconds=int(cfg.thresholds.get("solar_open_recent_window_seconds", 600)),
            voc_vmp_ratio=float(cfg.thresholds.get("solar_open_voc_vmp_ratio", 1.15)),
            float_state=int(cfg.thresholds.get("solar_open_float_state", 5)),
        )
        self.alarms = AlarmState()
        self.ac = AcState()
        self.solar = SolarState()
        self.bank: BankState | None = None
        self.last_heartbeat_ts: int = 0
        self.last_sample_ts: int = 0
        # When each on-change topic last actually arrived, as opposed to when
        # it was last written. _carry_forward_tick uses the gap between the two.
        self.last_batteries_msg_ts: int = 0
        self.last_temp_msg_ts: dict[int, int] = {}
        self.last_wifi_msg_ts: int = 0
        self.last_ac_sample_ts: int = 0
        self.last_ac_publish_ts: int = 0
        self.last_solar_sample_ts: int = 0
        self.last_solar_publish_ts: int = 0
        self.last_ble_tick_ts: int = 0
        self.last_obd_tick_ts: int = 0
        self.last_cabin_tick_ts: int = 0
        self.last_weather_tick_ts: int = 0
        self.last_govee_cloud_tick_ts: int = 0
        self.govee_api_key: str = os.environ.get("GOVEE_API_KEY", "").strip()
        if self.cfg.govee_cloud.enabled and not self.govee_api_key:
            log.warning("govee_cloud.enabled but GOVEE_API_KEY is not set — "
                        "cabin cloud fallback disabled")
        self.last_drives_tick_ts: int = 0
        self.last_heater_tick_ts: int = 0
        # instance -> {"soh": float, "capacity_ah": float}. Keyed by Victron
        # device instance because that is what the topic carries; the pack_id
        # mapping comes from the Batteries array, which is the only place the
        # uc token appears.
        self.pack_health: dict[int, dict[str, float]] = {}
        # instance -> {"temperature_c": float, "raw_value": float}. The two
        # arrive on separate topics and at different times, so they are paired
        # here rather than written as they land.
        self.temps: dict[int, dict[str, float]] = {}
        # Throttle per instance, not globally. Probes update independently, and
        # a shared stamp lets whichever one publishes first suppress the others
        # for a whole interval — which would silently drop a second sensor.
        self.last_temp_sample_ts: dict[int, int] = {}
        self.wifi: dict[str, object] = {}
        self.location = VanLocation(self.db.kv_mapping())
        self.last_wifi_sample_ts: int = 0
        self._was_online: bool | None = None
        self.stop_evt = threading.Event()

        # A monitor-measured bank is one entry, pack_id 1.
        pack_ids = ((1,) if cfg.bank.source == SOURCE_MONITOR
                    else tuple(sorted(set(cfg.packs.values()))))
        # Every name either source writes under; usually just "cabin".
        cabin_names = set(cfg.ble.cabin_sensors.values()) if cfg.ble.enabled else set()
        if cfg.govee_cloud.enabled:
            cabin_names.add(cfg.govee_cloud.sensor)
        self.cabin_sensors = tuple(sorted(cabin_names))
        self.mqtt = CerboMqtt(
            cfg.mqtt,
            on_batteries=self._on_batteries,
            on_heartbeat=self._on_heartbeat,
            on_alarm=self._on_alarm,
            on_ac=self._on_ac,
            on_solar=self._on_solar,
            on_battery_health=self._on_battery_health,
            on_temperature=self._on_temperature,
            on_wifi=self._on_wifi,
            on_location=self._on_location,
            # Cerberus's own topics go to HA's broker below, not the Cerbo.
            own_topics=False,
        )
        self.ha = HaBroker(
            cfg.ha_mqtt.host, cfg.ha_mqtt.port,
            availability_topic=f"{cfg.mqtt.publish_namespace}/availability",
            on_ready=self._on_ha_ready,
            subscriptions=cfg.ha_mqtt.import_topics,
            on_message=self._on_ha_import,
        ) if cfg.ha_mqtt.enabled else NoBroker()
        self.publisher = Publisher(
            mqtt=self.ha,
            namespace=cfg.mqtt.publish_namespace,
            discovery_prefix=cfg.mqtt.ha_discovery_prefix,
            pack_ids=pack_ids,
            monitor=cfg.bank.source == SOURCE_MONITOR,
            ble_enabled=cfg.ble.enabled,
            obd_enabled=cfg.obd.enabled,
            cabin_sensors=self.cabin_sensors,
        )
        self.syncer = Syncer(cfg.sync, self.db)
        # VRM cloud fallback (cerberus.vrm_cloud): a second, read-only client
        # that only exists while the local link is down.
        self.vrm_token: str = os.environ.get("VRM_TOKEN", "").strip()
        if cfg.mqtt.cloud_fallback and not self.vrm_token:
            log.warning("mqtt.cloud_fallback set but VRM_TOKEN is not — "
                        "cloud fallback disabled")
        self.cloud: CerboMqtt | None = None
        self._local_down_since: float | None = None
        self._cloud_next_try: float = 0.0

    def _on_ha_ready(self) -> None:
        """HA broker (re)connected: discovery first so entities exist before
        any state arrives, then the current availability — a retained
        "offline" from the LWT would otherwise stand until the feed changes."""
        online = self._cerbo_online()
        try:
            self.publisher.publish_discovery()
            self.publisher.publish_availability(online)
            self.publisher.publish_cerbo_online(online)
        except Exception:
            log.exception("HA ready publish failed")

    def _on_batteries(self, value: list[dict]) -> None:
        # Before any mapping: an unconfigured or wrongly configured bank is
        # exactly when setup detection is needed.
        self._inventory_tick(value)
        if self.cfg.bank.source == SOURCE_MONITOR:
            bank = parse_battery_monitor(value, self.cfg.bank.monitor_instance)
        else:
            bank = parse_batteries_array(value, self.cfg.packs)
        bank = self._merge_health(bank)
        if not bank.packs:
            log.debug("Batteries array contained no %s",
                      "battery monitor" if bank.source == SOURCE_MONITOR else "mapped packs")
            return
        self.bank = bank
        now = int(time.time())
        self.last_batteries_msg_ts = now
        self._check_system_voltage(bank)

        # 1. throttled persistence
        if now - self.last_sample_ts >= self.cfg.storage.sample_downsample_seconds:
            n = self.db.insert_pack_samples(bank.packs.values())
            self.last_sample_ts = now
            log.debug(
                "wrote %d pack samples; spread=%.1f%% total_current=%.2fA",
                n,
                bank.soc_spread or 0.0,
                bank.total_current,
            )

        # 2. detectors → events (persist + publish)
        try:
            events = self.detectors.evaluate(bank, now)
        except Exception:
            log.exception("detector evaluate failed")
            events = []

        for ev in events:
            self.db.insert_event(ev)
            try:
                self.publisher.publish_event(ev)
            except Exception:
                log.exception("publish_event failed")
            log.info("event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

        # 3. publish bank state
        try:
            self.publisher.publish_bank_state(bank)
            last_full = self.detectors.full_charge._read_int(
                detectors_mod.KV_LAST_FULL_CHARGE
            )
            self.publisher.publish_full_charge_state(
                last_full_charge_ts=last_full,
                days_since=self.detectors.full_charge.days_since_full_charge(now),
            )
        except Exception:
            log.exception("publish_bank_state failed")

    def _on_temperature(self, topic: str, value) -> None:
        """Persist the Cerbo's temperature probes, throttled like the others.

        Instance 24 is the reason this exists. It reads 67 C at 56 A of charge
        current, so it is measuring a joint rather than air, and which joint has
        never been established. Until now it lived only on VRM at 15-minute
        resolution, which is too coarse to correlate against a 30 s current
        trace — the correlation that would identify it.

        Writes every pack of readings on the storage downsample interval, not
        per message: Temperature and RawValue arrive separately and the tree
        updates faster than it needs recording.
        """
        parts = topic.split("/")
        try:
            instance = int(parts[-2])
        except (ValueError, IndexError):
            return
        if value is None:
            return
        try:
            reading = float(value)
        except (TypeError, ValueError):
            return
        field = {"Temperature": "temperature_c", "RawValue": "raw_value"}.get(parts[-1])
        if field is None:
            return
        vals = self.temps.setdefault(instance, {})
        vals[field] = reading
        self.last_temp_msg_ts[instance] = int(time.time())

        # Wait for the reading that matters. RawValue usually lands first, and
        # writing on it alone would put a null temperature in the table for
        # every probe's first row. Same rule _on_solar uses against a
        # half-populated tree at startup.
        if vals.get("temperature_c") is None:
            return

        now = int(time.time())
        if now - self.last_temp_sample_ts.get(instance, 0) < self.cfg.storage.sample_downsample_seconds:
            return
        try:
            self.db.insert_temperature_sample(
                ts=now,
                instance=instance,
                temperature_c=vals.get("temperature_c"),
                raw_value=vals.get("raw_value"),
            )
        except Exception:
            log.exception("insert_temperature_sample failed")
            return
        # Stamped only on a successful write, so a failed insert retries on the
        # next message instead of going quiet for an interval.
        self.last_temp_sample_ts[instance] = now

    def _phone_tick(self) -> None:
        """Take up the owner's phone position when the UI writes a new one
        (cerberus.phonefix, which already decided it is with the van)."""
        path = getattr(self, "config_path", None)
        if path is None:
            return
        try:
            mtime = phonefix.path_for(path).stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime == getattr(self, "_phone_mtime", None):
            return
        self._phone_mtime = mtime
        fix = phonefix.read(path)
        if fix is None:
            return
        first = self.location.phone is None
        self.location.phone_fix(fix["lat"], fix["lon"], fix["ts"])
        if first:
            log.info("van position from the owner's phone (via %s)", fix.get("via"))

    def _inventory_tick(self, value: list[dict]) -> None:
        """Record what the GX reports for setup detection (cerberus.detect):
        when it changes, else every REFRESH_SECONDS. Never fatal."""
        try:
            now = int(time.time())
            inv = detect_mod.inventory(now, value, self.pack_health, self.location)
            sig = detect_mod.signature(inv)
            if (sig == getattr(self, "_inventory_sig", None)
                    and now - getattr(self, "_inventory_ts", 0) < detect_mod.REFRESH_SECONDS):
                return
            self._inventory_sig, self._inventory_ts = sig, now
            self.db.set_kv(detect_mod.KV_INVENTORY, json.dumps(inv, separators=(",", ":")))
        except Exception:
            log.exception("setup inventory failed")

    def _on_location(self, topic: str, value) -> None:
        """GPS fix and the GX's time zone; see cerberus.location."""
        before = self.location.gx_timezone
        self.location.update(topic, value)
        if self.location.gx_timezone != before:
            log.info("GX time zone: %s", self.location.gx_timezone)

    def _snapshot_tick(self, online: bool) -> None:
        """Write the live view the UI reads (cerberus.live), every liveness tick.

        Swallows its own failures: the UI losing its live panel must never cost
        the loop that watches the Cerbo.
        """
        try:
            snap = live_mod.snapshot(
                int(time.time()), online, self.bank, self.solar, self.ac,
                capacity_fallback_ah=self.cfg.bank.capacity_ah,
            )
            self.db.set_kv(live_mod.KV_SNAPSHOT, live_mod.dumps(snap))
        except Exception:
            log.exception("live snapshot failed")

    def watch_config(self, path: Path) -> None:
        """Restart into new settings when config.yaml, the UI's setup.yaml or
        .env changes (see _config_changed)."""
        self.config_path = path
        self.restart_requested = False
        self._config_mtimes = self._mtimes()

    def _mtimes(self) -> tuple:
        return _mtimes(config_files(self.config_path))

    def _config_changed(self) -> bool:
        """True once the settings changed to something that loads: the loop
        then stops and main() re-executes this process with them. A change
        that does not load is logged and ignored, so a bad hand edit never
        takes the collector down."""
        if getattr(self, "config_path", None) is None:
            return False
        now = self._mtimes()
        if now == self._config_mtimes:
            return False
        self._config_mtimes = now
        try:
            cfg_mod.load(self.config_path)
        except Exception as err:      # ConfigError, YAML errors, a half-written file
            log.error("config changed but does not load, keeping the running settings: %s", err)
            return False
        log.warning("config changed; restarting to apply it")
        self.restart_requested = True
        return True

    def ensure_device_id(self) -> str:
        """A stable identity for this box, created once."""
        current = self.db.get_kv(KV_DEVICE_ID)
        if current:
            return current
        new = str(uuid.uuid4())
        self.db.set_kv(KV_DEVICE_ID, new)
        log.info("device id: %s", new)
        return new

    def _uptime_tick(self) -> None:
        """Leave evidence that the collector was running (see db
        collector_uptime and cerberus.health)."""
        now = int(time.time())
        if now - getattr(self, "last_uptime_ts", 0) >= UPTIME_PERIOD:
            self.last_uptime_ts = now
            self.db.mark_uptime(now)

    def van_tz(self):
        """The van's time zone for the nightly jobs: config, else GX, else host."""
        return self.location.timezone(self.cfg.timezone)

    def _on_wifi(self, topic: str, value) -> None:
        """Persist the Cerbo's own WiFi signal, throttled like the others.

        Writes only when a SignalStrength is known, and only from messages, so
        nothing is recorded while the Cerbo is off the network: the gap in this
        table lines up with the gap in every other one, and the rows just
        before it show whether the signal sagged first.
        """
        field = topic.rsplit("/", 1)[-1]
        if field == "SignalStrength":
            try:
                self.wifi["signal_strength"] = None if value is None else int(value)
            except (TypeError, ValueError):
                return
        elif field == "State":
            self.wifi["state"] = None if value is None else str(value)
        else:
            return
        self.last_wifi_msg_ts = int(time.time())
        if self.wifi.get("signal_strength") is None:
            return
        now = int(time.time())
        if now - self.last_wifi_sample_ts < self.cfg.storage.sample_downsample_seconds:
            return
        if self.db.insert_cerbo_wifi_sample(
            now, self.wifi.get("signal_strength"), self.wifi.get("state")
        ):
            self.last_wifi_sample_ts = now

    def _on_battery_health(self, topic: str, value) -> None:
        """Cache Soh / Capacity from N/<portal>/battery/<inst>/{Soh,Capacity}.

        These arrive on their own topics, asynchronously and far less often
        than the Batteries array, so they are cached by instance and merged at
        write time rather than triggering a write themselves.
        """
        if value is None:
            return
        parts = topic.split("/")
        try:
            instance = int(parts[-2])
            reading = float(value)
        except (ValueError, IndexError):
            return
        field = "soh" if parts[-1] == "Soh" else "capacity_ah"
        self.pack_health.setdefault(instance, {})[field] = reading

    def _merge_health(self, bank: BankState) -> BankState:
        """Fold cached Soh/Capacity onto each pack by device instance.

        Packs never heard from stay None — an unknown SoH must not be written
        as 0.0, which would read as a dead pack. Same "absent is not zero" rule
        the solar detector depends on.
        """
        if not self.pack_health:
            return bank
        merged = {}
        for pid, pack in bank.packs.items():
            health = self.pack_health.get(pack.instance)
            merged[pid] = (
                replace(pack, soh=health.get("soh"), capacity_ah=health.get("capacity_ah"))
                if health else pack
            )
        return replace(bank, packs=merged)

    def _check_system_voltage(self, bank: BankState) -> None:
        """Warn once if the bank reads outside the configured system voltage.

        Every bank-side alarm level comes from system.nominal_voltage, so a
        24 V bank left on the 12 V default would alarm on nothing and miss
        everything, silently. 0 V readings are a missing value (see
        LowChargeDetector), not evidence either way.
        """
        if getattr(self, "_system_voltage_warned", False):
            return
        prof = self.cfg.system
        off = [p.voltage for p in bank.packs.values()
               if p.voltage >= 5.0 and not prof.plausible(p.voltage)]
        if off:
            self._system_voltage_warned = True
            log.warning(
                "bank reads %.2f V, outside %.1f-%.1f V for a %d V %s system — "
                "check system.nominal_voltage in config.yaml; alarm levels are wrong",
                off[0], prof.plausible_min_v, prof.plausible_max_v,
                prof.nominal_v, prof.chemistry,
            )

    def _check_array_voltage(self) -> None:
        """Warn once if PV reads well above the configured array Voc.

        The solar-open thresholds come from solar.array_voc_v, so a series
        string left on the 20 V default would sit permanently above the
        "sun is up" and "open circuit" levels. 25% of margin, because cold
        panels legitimately run 10-15% over their warm-weather Voc (this rig:
        21.9 V against ~20 V).
        """
        if getattr(self, "_array_voltage_warned", False):
            return
        primary = self.solar.primary
        pv_v = getattr(primary, "pv_voltage", None)
        voc = self.cfg.solar.array_voc_v
        if pv_v is not None and pv_v > voc * ARRAY_VOC_MARGIN:
            self._array_voltage_warned = True
            log.warning(
                "PV reads %.1f V, above %.1f V (array_voc_v %.1f V + %d%%) — check "
                "solar.array_voc_v in config.yaml; solar-open thresholds are wrong",
                pv_v, voc * ARRAY_VOC_MARGIN, voc, round((ARRAY_VOC_MARGIN - 1) * 100),
            )

    def _on_heartbeat(self, value: int) -> None:
        self.last_heartbeat_ts = int(time.time())

    def _on_alarm(self, topic: str, value: int | None) -> None:
        alarm_id = parse_alarm_topic(topic)
        if alarm_id is None:
            return
        events = self.alarms.update(alarm_id, value, int(time.time()))
        for ev in events:
            self.db.insert_event(ev)
            try:
                self.publisher.publish_event(ev)
            except Exception:
                log.exception("publish_event(alarm) failed")
            log.info("alarm event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

    def _on_ac(self, topic: str, value) -> None:
        """Fold an AC topic into AcState, then persist/publish on a throttle.

        The AC tree is chatty — Power and Current for every phase, updated
        continuously — so unlike the Batteries array this fires many times a
        second. Folding is cheap; writing is not, so persistence reuses the
        battery downsample interval and publishing gets its own shorter one
        (an AC load needs to show up in HA promptly, but not at wire rate).
        """
        if not self.ac.update(topic, value):
            return
        now = int(time.time())

        if now - self.last_ac_publish_ts >= AC_PUBLISH_PERIOD:
            self.last_ac_publish_ts = now
            try:
                self.publisher.publish_ac_state(self.ac)
            except Exception:
                log.exception("publish_ac_state failed")

        if now - self.last_ac_sample_ts >= self.cfg.storage.sample_downsample_seconds:
            consumption = self.ac.total_consumption_w
            input_w = self.ac.total_input_w
            output_w = self.ac.total_output_w
            # Write when something real is known. Two cases qualify:
            #
            #   * a power figure is present — the normal running case;
            #   * every power is null but the inverter state is known, which
            #     is exactly what a switched-off inverter looks like on the
            #     wire. The Cerbo publishes null for every AC power path in
            #     that condition; only vebus/<inst>/State carries a value.
            #
            # Without the second case the table simply stops while the
            # inverter is off, leaving "off" indistinguishable from "the
            # collector wasn't running" — the silent-gap failure mode that has
            # already cost this project two investigations.
            #
            # The original concern still holds and is still handled: the first
            # AC message cannot write a row of nulls, because inverter_state
            # is only set once vebus/<inst>/State has actually arrived.
            if (any(v is not None for v in (consumption, input_w, output_w))
                    or self.ac.inverter_state is not None):
                self.last_ac_sample_ts = now
                try:
                    self.db.insert_ac_sample(
                        ts=now,
                        consumption_w=consumption,
                        input_w=input_w,
                        output_w=output_w,
                        shore_connected=self.ac.shore_connected,
                        inverter_state=self.ac.inverter_state,
                        inverter_dc_voltage=self.ac.dc_voltage,
                        inverter_dc_current=self.ac.dc_current,
                    )
                except Exception:
                    log.exception("insert_ac_sample failed")

    def _on_solar(self, topic: str, value) -> None:
        """Fold a solar topic into SolarState, then persist/publish throttled.

        Same shape as _on_ac: the solarcharger tree updates continuously, so
        folding happens per message but writing does not.
        """
        if not self.solar.update(topic, value):
            return
        now = int(time.time())
        self._check_array_voltage()

        if now - self.last_solar_publish_ts >= SOLAR_PUBLISH_PERIOD:
            self.last_solar_publish_ts = now
            try:
                self.publisher.publish_solar_state(self.solar)
            except Exception:
                log.exception("publish_solar_state failed")

            # Same cadence as the publish — the detector's own sustain window
            # does the debouncing, so there is nothing to gain from running it
            # on every message of the tree.
            try:
                events = self.solar_open_detector.evaluate(self.solar, self.bank, now)
            except Exception:
                log.exception("solar_open_detector evaluate failed")
                events = []
            for ev in events:
                self.db.insert_event(ev)
                try:
                    self.publisher.publish_event(ev)
                except Exception:
                    log.exception("publish_event(solar) failed")
                log.warning("event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

        if now - self.last_solar_sample_ts >= self.cfg.storage.sample_downsample_seconds:
            pv_power = self.solar.total_pv_power
            amps = self.solar.total_charge_current
            primary = self.solar.primary
            # Don't write a row of nulls from the first message to arrive,
            # before the rest of the tree has been seen.
            if pv_power is not None or amps is not None:
                self.last_solar_sample_ts = now
                try:
                    self.db.insert_solar_sample(
                        ts=now,
                        pv_power=pv_power,
                        pv_voltage=primary.pv_voltage if primary else None,
                        charge_current=amps,
                        yield_today_kwh=self.solar.total_yield_today_kwh,
                        state=primary.state if primary else None,
                        error_code=primary.error_code if primary else None,
                        dc_voltage=primary.dc_voltage if primary else None,
                    )
                except Exception:
                    log.exception("insert_solar_sample failed")

    def _carry_forward_tick(self) -> None:
        """Write unchanged on-change topics on the normal sample interval.

        The GX publishes Batteries, temperatures and WiFi only when a value
        changes. Since the keepalive stopped forcing a full republish every
        30 s (PR #10), a quiet night left battery_samples with 2-4 minute holes
        (10-14/15: 1086 of 1440 rows) while the feed was demonstrably alive.
        With the heartbeat fresh, no message means no change, so the last
        value is still the reading. Two limits keep that honest:
          * the caller only runs this while _cerbo_online(), so real outages
            still leave gaps;
          * a topic silent for longer than two full republishes is not merely
            unchanged (each full republish would have resent it), so it stops
            being carried and the gap shows.
        """
        now = int(time.time())
        step = self.cfg.storage.sample_downsample_seconds
        max_quiet = 2 * self.cfg.mqtt.full_republish_seconds + LIVENESS_CHECK_PERIOD

        if (self.bank is not None
                and now - self.last_sample_ts >= step
                and now - self.last_batteries_msg_ts <= max_quiet):
            packs = [replace(p, ts=now) for p in self.bank.packs.values()]
            if self.db.insert_pack_samples(packs):
                self.last_sample_ts = now

        # list(): the paho thread can add a probe mid-iteration.
        for instance, vals in list(self.temps.items()):
            if (vals.get("temperature_c") is not None
                    and now - self.last_temp_sample_ts.get(instance, 0) >= step
                    and now - self.last_temp_msg_ts.get(instance, 0) <= max_quiet):
                try:
                    self.db.insert_temperature_sample(
                        ts=now,
                        instance=instance,
                        temperature_c=vals.get("temperature_c"),
                        raw_value=vals.get("raw_value"),
                    )
                except Exception:
                    log.exception("insert_temperature_sample failed")
                    continue
                self.last_temp_sample_ts[instance] = now

        if (self.wifi.get("signal_strength") is not None
                and now - self.last_wifi_sample_ts >= step
                and now - self.last_wifi_msg_ts <= max_quiet):
            if self.db.insert_cerbo_wifi_sample(
                now, self.wifi.get("signal_strength"), self.wifi.get("state")
            ):
                self.last_wifi_sample_ts = now

    def _cerbo_online(self) -> bool:
        if self.last_heartbeat_ts == 0:
            return False
        return (int(time.time()) - self.last_heartbeat_ts) < HEARTBEAT_STALE_SECONDS

    def run(self) -> None:
        self.ha.start()
        self.mqtt.start()
        self.syncer.start()
        try:
            while not self.stop_evt.wait(LIVENESS_CHECK_PERIOD):
                online = self._cerbo_online()
                if online != self._was_online:
                    log.warning("Cerbo %s", "online" if online else "offline")
                    try:
                        self.publisher.publish_availability(online)
                        self.publisher.publish_cerbo_online(online)
                    except Exception:
                        log.exception("publish_cerbo_online failed")
                    self._was_online = online
                if should_force_reconnect(
                    cerbo_online=online,
                    ever_saw_heartbeat=self.last_heartbeat_ts > 0,
                    mqtt_connected=self.mqtt.is_connected,
                ):
                    # Safe from here: this is the main loop, not a paho
                    # callback thread, so the loop_stop() join can't deadlock.
                    self.mqtt.force_reconnect("no Cerbo heartbeat")
                if self.cfg.mqtt.cloud_fallback and self.vrm_token:
                    self._cloud_tick()
                if online:
                    self._carry_forward_tick()
                if self.cfg.ble.enabled:
                    self._ble_tick()
                if self.cfg.obd.enabled:
                    self._obd_tick()
                if self.cabin_sensors:
                    self._cabin_tick()
                if self.cfg.weather.enabled:
                    self._weather_tick()
                if self.cfg.govee_cloud.enabled and self.govee_api_key:
                    self._govee_cloud_tick()
                if self.cfg.drives.enabled:
                    self._drives_tick()
                self._heater_tick()
                self._phone_tick()
                self._snapshot_tick(online)
                self._uptime_tick()
                if self._config_changed():
                    break
        finally:
            self.syncer.stop()
            if self.cloud is not None:
                self.cloud.stop()
            self.mqtt.stop()
            self.ha.stop()
            self.db.close()

    def _cloud_tick(self, now: float | None = None) -> None:
        """Run the VRM cloud relay while, and only while, the LAN link is down.

        Started once the local client has been disconnected for
        cloud_after_seconds — long enough for host rotation to have had its
        go — and stopped the moment it reconnects, so the two never feed the
        handlers together for more than a tick. Nothing is published to the
        cloud broker; HA keeps getting state only when the local link (and
        its bridge) is up.
        """
        now = time.time() if now is None else now
        if self.mqtt.is_connected:
            self._local_down_since = None
            if self.cloud is not None:
                log.warning("local MQTT back; stopping VRM cloud fallback")
                self.cloud.stop()
                self.cloud = None
            return
        if self._local_down_since is None:
            self._local_down_since = now
        if (self.cloud is not None
                or now - self._local_down_since < self.cfg.mqtt.cloud_after_seconds
                or now < self._cloud_next_try):
            return
        try:
            email = vrm_cloud.account_email(self.vrm_token)
            ccfg = vrm_cloud.cloud_config(self.cfg.mqtt, self.vrm_token, email)
            self.cloud = CerboMqtt(
                ccfg,
                on_batteries=self._on_batteries,
                on_heartbeat=self._on_heartbeat,
                on_alarm=self._on_alarm,
                on_ac=self._on_ac,
                on_solar=self._on_solar,
                on_battery_health=self._on_battery_health,
                on_temperature=self._on_temperature,
                on_wifi=self._on_wifi,
                on_location=self._on_location,
                tls_context=vrm_cloud.tls_context(),
                own_topics=False,
            )
            self.cloud.start()
            log.warning("local MQTT down %.0fs; VRM cloud fallback started (%s)",
                        now - self._local_down_since, ccfg.host)
        except Exception:
            log.exception("VRM cloud fallback failed to start; retrying in %ds",
                          CLOUD_RETRY_SECONDS)
            self.cloud = None
            self._cloud_next_try = now + CLOUD_RETRY_SECONDS

    def _drives_tick(self) -> None:
        """Record and report drives that have finished since the last check.

        Each finished drive is compared against its baseline (cerberus.drives)
        and becomes one drive_report event: info when normal, warn when
        anything was flagged. The HA automation turns that into a one-line
        phone notification; the full report is a markdown file beside the DB.
        The first run backfills history silently.
        """
        now = int(time.time())
        if now - self.last_drives_tick_ts < DRIVES_TICK_PERIOD:
            return
        self.last_drives_tick_ts = now
        report_dir = Path(self.cfg.drives.report_dir)
        if not report_dir.is_absolute():
            report_dir = Path(self.cfg.storage.sqlite_path).parent / report_dir
        events = []
        try:
            events += drives_mod.process(
                self.db, now, report_dir,
                epochs=self.cfg.drives.baseline_epochs,
                settings=replace(self.cfg.drives.settings, tz=self.van_tz()),
            )
        except Exception:
            log.exception("drive processing failed")
        # Same cadence and baseline epochs: the daily solar-path check is the
        # charge-path counterpart of the per-drive one.
        try:
            events += solar_path_mod.process(
                self.db, now, epochs=self.cfg.drives.baseline_epochs,
                settings=replace(self.cfg.solar_path, tz=self.van_tz()),
            )
        except Exception:
            log.exception("solar path processing failed")
        for ev in events:
            self.db.insert_event(ev)
            try:
                self.publisher.publish_event(ev)
            except Exception:
                log.exception("publish_event failed")
            log.info("event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

    def _heater_tick(self) -> None:
        """Report water-heater runs from the AC load (cerberus.heater).

        heater_on when a run starts, heater_run with its figures when it ends.
        """
        now = int(time.time())
        if now - self.last_heater_tick_ts < HEATER_TICK_PERIOD:
            return
        self.last_heater_tick_ts = now
        try:
            events = heater_mod.process(self.db, now, replace(self.cfg.heater, tz=self.van_tz()))
        except Exception:
            log.exception("heater processing failed")
            return
        for ev in events:
            self.db.insert_event(ev)
            try:
                self.publisher.publish_event(ev)
            except Exception:
                log.exception("publish_event failed")
            log.info("event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

    def _weather_tick(self) -> None:
        """Fetch outdoor conditions and store them next to the electrical data.

        Deliberately does nothing else — no publish, no detector. This is here
        to accumulate the ambient history that does not exist anywhere else:
        HA's recorder drops weather states after ``purge_keep_days`` and
        weather attributes produce no long-term statistics, so the seasonal
        question ("does load fall as fast as solar does?") has no data behind
        it until this table has a season in it.

        Every failure path is swallowed. A missing weather row is a gap in a
        nice-to-have series; an exception escaping here would take down the
        liveness loop that watches the Cerbo, which is the job that matters.
        """
        now = int(time.time())
        if now - self.last_weather_tick_ts < self.cfg.weather.interval_seconds:
            return
        # Stamp the attempt, not the success: a network failure must not turn
        # into a retry every 5 s against a third-party API.
        self.last_weather_tick_ts = now
        lat, lon, where = self.location.position(
            self.cfg.weather.latitude, self.cfg.weather.longitude, now)
        if where == "config" and lat == 0.0 and lon == 0.0:
            # A new box with no GPS, phone fix or home ZIP yet: there is no
            # "here" to forecast. Say so once, not every 15 minutes.
            if not getattr(self, "_weather_no_position_logged", False):
                self._weather_no_position_logged = True
                log.info("weather waits for a position: set the home ZIP code in Settings")
            return
        try:
            sample = fetch_weather(
                lat,
                lon,
                self.cfg.weather.user_agent,
                timeout=self.cfg.weather.request_timeout_seconds,
            )
        except Exception:
            log.exception("weather fetch raised")
            return
        if sample is None:
            return
        try:
            self.db.insert_weather_sample(
                now,
                air_temp_f=sample.air_temp_f,
                cloud_pct=sample.cloud_pct,
                humidity_pct=sample.humidity_pct,
                wind_mph=sample.wind_mph,
                lat=lat,
                lon=lon,
                position_source=where,
            )
        except Exception:
            log.exception("insert_weather_sample failed")

    def _govee_cloud_tick(self) -> None:
        """Store the cabin sensor's cloud reading, the fallback for BLE range.

        Same failure isolation as _weather_tick: every error is swallowed so a
        third-party API can never take down the loop that watches the Cerbo.
        """
        now = int(time.time())
        g = self.cfg.govee_cloud
        if now - self.last_govee_cloud_tick_ts < g.interval_seconds:
            return
        # Stamp the attempt, not the success — no 5 s retry loop on failure.
        self.last_govee_cloud_tick_ts = now
        try:
            reading = fetch_govee_cloud(
                self.govee_api_key, g.sku, g.device, timeout=g.request_timeout_seconds,
            )
        except Exception:
            log.exception("govee cloud fetch raised")
            return
        if reading is None:
            return
        try:
            self.db.insert_cabin_sample(
                now, g.sensor,
                air_temp_f=reading.air_temp_f,
                humidity_pct=reading.humidity_pct,
                battery_pct=None,
                source="govee_cloud",
            )
        except Exception:
            log.exception("insert_cabin_sample (govee cloud) failed")

    def _on_ha_import(self, topic: str, payload: bytes) -> None:
        """A vehicle snapshot from ha_mqtt.import_topics (cerberus.vehicle).
        Runs on the HA client's thread; Db gives each thread its own
        connection."""
        snap = vehicle_mod.parse_snapshot(payload)
        if snap is None:
            log.warning("ha import: %s is not a vehicle snapshot; ignored", topic)
            return
        ts, source, values = snap
        n = self.db.insert_vehicle_samples(ts, source, values)
        log.debug("ha import: %d values from %s (%s)", n, source, topic)

    def _obd_tick(self) -> None:
        """Read the latest engine row from SQLite and publish it.

        Same sidecar-writes/collector-publishes split as _ble_tick, for the
        same reason: `cerberus.ble` holds the radio and never speaks MQTT.
        Absence is normal and is not an error — the table stops whenever the
        ignition is off or the laptop is out of the van.
        """
        now = int(time.time())
        if now - self.last_obd_tick_ts < OBD_TICK_PERIOD:
            return
        self.last_obd_tick_ts = now
        try:
            sample = self.db.latest_engine_sample(
                max_age_seconds=OBD_FRESHNESS_SECONDS
            )
        except Exception:
            log.exception("latest_engine_sample failed")
            return
        if sample is None:
            return
        try:
            self.publisher.publish_engine_state(sample)
        except Exception:
            log.exception("publish_engine_state failed")

    def _cabin_tick(self) -> None:
        """Read the newest cabin row per sensor from SQLite and publish it.

        Same sidecar-writes/collector-publishes split as _obd_tick: the BLE
        sidecar and the cloud poller fill cabin_samples, only this process
        talks to HA.
        """
        now = int(time.time())
        if now - self.last_cabin_tick_ts < CABIN_TICK_PERIOD:
            return
        self.last_cabin_tick_ts = now
        try:
            samples = self.db.latest_cabin_per_sensor(
                max_age_seconds=CABIN_FRESHNESS_SECONDS
            )
        except Exception:
            log.exception("latest_cabin_per_sensor failed")
            return
        try:
            self.publisher.publish_cabin_state(samples)
        except Exception:
            log.exception("publish_cabin_state failed")

    def _ble_tick(self) -> None:
        """Read latest BLE samples from SQLite, publish derived stats, run the
        cell-imbalance detector. Runs at the same cadence as the liveness
        loop; cheap (one query per pack)."""
        now = int(time.time())
        if now - self.last_ble_tick_ts < BLE_TICK_PERIOD:
            return
        self.last_ble_tick_ts = now
        try:
            samples = self.db.latest_ble_per_pack(max_age_seconds=BLE_FRESHNESS_SECONDS)
        except Exception:
            log.exception("latest_ble_per_pack failed")
            return
        if not samples:
            return
        try:
            self.publisher.publish_ble_state(samples)
        except Exception:
            log.exception("publish_ble_state failed")
        cells = {pid: s.cells_mv for pid, s in samples.items() if s.cells_mv}
        if not cells:
            return
        try:
            events = self.cell_detector.evaluate(cells, now)
        except Exception:
            log.exception("cell_detector evaluate failed")
            return
        for ev in events:
            self.db.insert_event(ev)
            try:
                self.publisher.publish_event(ev)
            except Exception:
                log.exception("publish_event(cell) failed")
            log.info("event: %s/%s %s", ev.severity.value, ev.type, ev.detail)

    def shutdown(self, *_args) -> None:
        log.info("shutdown signal received")
        self.stop_evt.set()


def config_files(config_path: Path) -> tuple[Path, ...]:
    """What the settings are made of: config.yaml, the UI's setup.yaml, and
    .env (the Cerbo password, which first-run setup and the UI can change)."""
    return (config_path, cfg_mod.setup_path(config_path), envfile.path_for(config_path))


def _mtimes(paths) -> tuple:
    out = []
    for p in paths:
        try:
            out.append(p.stat().st_mtime_ns)
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


def reexec(config_path: Path, module: str = "cerberus") -> None:
    """Replace this process with a fresh collector: same PID and arguments,
    so launchd / systemd keep supervising it. The environment is inherited,
    so a password changed in .env since this process started is copied in
    first, or the stale one would win (envfile.mqtt_password)."""
    pw = envfile.read(envfile.path_for(config_path)).get(envfile.MQTT_PASSWORD)
    if pw:
        os.environ[envfile.MQTT_PASSWORD] = pw
    os.execv(sys.executable, [sys.executable, "-m", module, *sys.argv[1:]])


def wait_for_config(config_path: Path, reason: Exception, poll_s: float = 5.0,
                    stop=None, waiting: str | None = None) -> bool:
    """A box not set up yet (or with a config that doesn't load) waits here
    instead of exiting, so the service manager doesn't restart it in a loop.
    True once the settings change into something that loads."""
    if waiting:
        log.warning(waiting)
    elif str(reason) == cfg_mod.NOT_SET_UP or isinstance(reason, FileNotFoundError):
        log.warning("waiting for first-run setup: open the Cerberus UI's Settings to connect the Cerbo")
    else:
        log.error("config error, waiting for it to be fixed: %s", reason)
    stop = stop or threading.Event()
    files = config_files(config_path)
    seen = _mtimes(files)
    while not stop.wait(poll_s):
        now = _mtimes(files)
        if now == seen:
            continue
        seen = now
        try:
            cfg_mod.load(config_path)
        except Exception as err:          # still incomplete: setup writes several files
            log.info("settings changed but don't load yet: %s", err)
            continue
        log.warning("settings now load; starting")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="cerberus")
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)

    try:
        cfg = cfg_mod.load(args.config)
    except (cfg_mod.ConfigError, FileNotFoundError, yaml.YAMLError) as e:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        if wait_for_config(args.config, e, stop=stop):
            reexec(args.config)
        return 0

    app = Cerberus(cfg)
    app.watch_config(args.config)
    app.ensure_device_id()
    signal.signal(signal.SIGINT, app.shutdown)
    signal.signal(signal.SIGTERM, app.shutdown)
    log.info("Cerberus starting (M2)")
    # The restart marker cerberus.health uses to tell "restarted after being
    # off" from "was asleep". Stored only; not published as a notification.
    app.db.insert_event(make_event("collector_started", Severity.INFO, {}))
    app.run()
    log.info("Cerberus stopped")
    if app.restart_requested:
        reexec(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
