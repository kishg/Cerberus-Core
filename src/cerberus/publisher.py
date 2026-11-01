"""Home Assistant MQTT discovery + retained state-topic publisher.

HA's MQTT Discovery auto-creates entities when we publish a retained config
under ``<discovery_prefix>/<component>/cerberus/<object>/config``. State lives
on ``<namespace>/...`` topics, all retained so HA sees the last known value at
startup. Availability is mirrored via the LWT topic on ``mqtt_client``.

Output here is fire-and-forget; we never wait for HA. Cerberus does not write
back to the Cerbo — these topics are the only way findings reach the user.
"""

from __future__ import annotations

import json
import importlib.metadata
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .ac import AcState
from .db import BlePackSample, CabinSample, EngineSample
from .events import Event
from .model import BankState
from .mqtt_client import CerboMqtt
from .solar import SolarState

log = logging.getLogger(__name__)

def _version() -> str:
    try:
        return importlib.metadata.version("cerberus")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# identifiers and name stay as they were: HA keys the device (and every
# entity's place under it) on them, so changing either would orphan entities.
DEVICE = {
    "identifiers": ["cerberus"],
    "name": "Cerberus",
    "manufacturer": "Cerberus (open source)",
    "model": "Van electrical monitor",
    "sw_version": _version(),
}


@dataclass
class Publisher:
    mqtt: CerboMqtt
    namespace: str
    discovery_prefix: str
    pack_ids: tuple[int, ...]
    # A bank measured by one monitor (bank.source monitor) is pack 1 in the
    # data, but "Pack 1" would be wrong in HA: its entities are named for the
    # battery. Their unique_ids (pack1_*) stay, so topics and history match.
    monitor: bool = False
    ble_enabled: bool = False
    obd_enabled: bool = False
    cabin_sensors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.availability_topic = f"{self.namespace}/availability"

    def _label(self, pid: int, what: str) -> str:
        return f"Battery {what}" if self.monitor else f"Pack {pid} {what}"

    # -- discovery (one-shot on connect) -------------------------------------

    def publish_discovery(self) -> None:
        ns = self.namespace
        ds = []  # (component, object_id, payload)

        # has_entity_name + short names: HA derives entity_id from the device
        # name + the entity name. Friendly name shows "Cerberus SoC Spread"
        # consistently across all entities, and entity_id is exactly
        # `sensor.cerberus_soc_spread` (no redundant "Battery" or "Last" baked
        # in by an over-long name slug — that was the 2026-06-08 dashboard
        # rename pain).

        if not self.monitor:                  # one battery: always 0
            ds.append(self._sensor(
                object_id="soc_spread",
                name="SoC Spread",
                state_topic=f"{ns}/battery/soc_spread",
                unit="%",
                icon="mdi:battery-alert",
            ))
        ds.append(self._sensor(
            object_id="days_since_full_charge",
            name="Days Since Full Charge",
            state_topic=f"{ns}/battery/days_since_full_charge",
            unit="d",
            icon="mdi:calendar-clock",
        ))
        ds.append(self._sensor(
            object_id="last_full_charge",
            name="Last Full Charge",
            state_topic=f"{ns}/battery/last_full_charge",
            device_class="timestamp",
            state_class=None,
            icon="mdi:battery-charging-100",
        ))

        for pid in self.pack_ids:
            ds.append(self._sensor(
                object_id=f"pack{pid}_soc",
                name=self._label(pid, "SoC"),
                state_topic=f"{ns}/battery/pack{pid}/soc",
                unit="%",
                device_class="battery",
            ))
            ds.append(self._sensor(
                object_id=f"pack{pid}_voltage",
                name=self._label(pid, "Voltage"),
                state_topic=f"{ns}/battery/pack{pid}/voltage",
                unit="V",
                device_class="voltage",
            ))
            ds.append(self._sensor(
                object_id=f"pack{pid}_current",
                name=self._label(pid, "Current"),
                state_topic=f"{ns}/battery/pack{pid}/current",
                unit="A",
                device_class="current",
            ))
            ds.append(self._sensor(
                object_id=f"pack{pid}_temperature",
                name=self._label(pid, "Temperature"),
                state_topic=f"{ns}/battery/pack{pid}/temperature",
                unit="°C",
                device_class="temperature",
            ))
            if not self.monitor:              # one battery: always 100%
                ds.append(self._sensor(
                    object_id=f"current_share_pack{pid}",
                    name=f"Pack {pid} Current Share",
                    state_topic=f"{ns}/battery/current_share/pack{pid}",
                    unit="%",
                    icon="mdi:percent",
                ))

        ds.append(self._binary_sensor(
            object_id="cerbo_online",
            name="Cerbo Online",
            state_topic=f"{ns}/cerbo_online",
            device_class="connectivity",
        ))

        # AC side. power device_class + measurement state_class means HA can
        # feed these straight into an energy dashboard / long-term statistics.
        # The three AC power figures go null whenever the inverter is off, so
        # each carries its own availability gate. Without it the retained
        # state topic keeps serving the last live number indefinitely — HA
        # showed "AC Consumption 178W" for 40 hours with the inverter off.
        ds.append(self._sensor(
            object_id="ac_consumption_power",
            name="AC Consumption",
            state_topic=f"{ns}/ac/consumption_power",
            unit="W",
            device_class="power",
            extra_availability=f"{ns}/ac/consumption_power/avail",
        ))
        ds.append(self._sensor(
            object_id="ac_input_power",
            name="AC Input Power",
            state_topic=f"{ns}/ac/input_power",
            unit="W",
            device_class="power",
            extra_availability=f"{ns}/ac/input_power/avail",
        ))
        ds.append(self._sensor(
            object_id="ac_output_power",
            name="Inverter AC Output",
            state_topic=f"{ns}/ac/output_power",
            unit="W",
            device_class="power",
            extra_availability=f"{ns}/ac/output_power/avail",
        ))
        ds.append(self._sensor(
            object_id="inverter_state",
            name="Inverter State",
            state_topic=f"{ns}/ac/inverter_state",
            state_class=None,          # a label, not a measurement
            icon="mdi:power-plug",
        ))
        ds.append(self._binary_sensor(
            object_id="shore_power",
            name="Shore Power",
            state_topic=f"{ns}/ac/shore_connected",
            device_class="plug",
        ))

        # Solar. Yield Today is total_increasing (it resets at midnight), which
        # is what HA's energy dashboard expects from a daily counter.
        ds.append(self._sensor(
            object_id="pv_power",
            name="PV Power",
            state_topic=f"{ns}/solar/pv_power",
            unit="W",
            device_class="power",
        ))
        ds.append(self._sensor(
            object_id="pv_voltage",
            name="PV Voltage",
            state_topic=f"{ns}/solar/pv_voltage",
            unit="V",
            device_class="voltage",
        ))
        ds.append(self._sensor(
            object_id="solar_charge_current",
            name="Solar Charge Current",
            state_topic=f"{ns}/solar/charge_current",
            unit="A",
            device_class="current",
        ))
        ds.append(self._sensor(
            object_id="solar_yield_today",
            name="Solar Yield Today",
            state_topic=f"{ns}/solar/yield_today",
            unit="kWh",
            device_class="energy",
            state_class="total_increasing",
        ))
        ds.append(self._sensor(
            object_id="solar_state",
            name="Solar Charger State",
            state_topic=f"{ns}/solar/state",
            state_class=None,
            icon="mdi:solar-power",
        ))
        ds.append(self._binary_sensor(
            object_id="solar_error",
            name="Solar Charger Fault",
            state_topic=f"{ns}/solar/fault",
            device_class="problem",
        ))

        if self.ble_enabled:
            ds.extend(self._ble_discovery_entities(ns))
        if self.obd_enabled:
            ds.extend(self._obd_discovery_entities(ns))
        ds.extend(self._cabin_discovery_entities(ns))

        for component, object_id, payload in ds:
            topic = f"{self.discovery_prefix}/{component}/cerberus/{object_id}/config"
            self.mqtt.publish(topic, json.dumps(payload), retain=True, qos=1)
        log.info("published %d HA discovery configs", len(ds))

    def _sensor(
        self,
        object_id: str,
        name: str,
        state_topic: str,
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = "measurement",
        icon: str | None = None,
        extra_availability: str | None = None,
    ) -> tuple[str, str, dict]:
        cfg: dict = {
            "name": name,
            "has_entity_name": True,
            "unique_id": f"cerberus_{object_id}",
            "state_topic": state_topic,
            "device": DEVICE,
        }
        if extra_availability is None:
            cfg["availability_topic"] = self.availability_topic
        else:
            # HA treats availability_topic and availability as mutually
            # exclusive, so a per-entity gate has to restate the global one.
            # mode "all" => the entity is available only when Cerberus is up
            # AND this particular reading is currently being reported.
            cfg["availability"] = [
                {"topic": self.availability_topic},
                {
                    "topic": extra_availability,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                },
            ]
            cfg["availability_mode"] = "all"
        if unit:
            cfg["unit_of_measurement"] = unit
        if device_class:
            cfg["device_class"] = device_class
        if state_class:
            cfg["state_class"] = state_class
        if icon:
            cfg["icon"] = icon
        return ("sensor", object_id, cfg)

    def _binary_sensor(
        self,
        object_id: str,
        name: str,
        state_topic: str,
        device_class: str | None = None,
    ) -> tuple[str, str, dict]:
        cfg: dict = {
            "name": name,
            "has_entity_name": True,
            "unique_id": f"cerberus_{object_id}",
            "state_topic": state_topic,
            "availability_topic": self.availability_topic,
            "payload_on": "on",
            "payload_off": "off",
            "device": DEVICE,
        }
        if device_class:
            cfg["device_class"] = device_class
        return ("binary_sensor", object_id, cfg)

    # -- state -----------------------------------------------------------------

    def publish_bank_state(self, bank: BankState) -> None:
        ns = self.namespace
        if bank.soc_spread is not None:
            self.mqtt.publish(f"{ns}/battery/soc_spread", f"{bank.soc_spread:.1f}", retain=True)

        # True bank aggregates. The Cerbo's system/0/Dc/Battery/{Power,Current}
        # only ever reflect the active-battery-service pack, so consumers that
        # want the whole bank have to read these instead. See BankState.total_power.
        self.mqtt.publish(f"{ns}/battery/bank_current", f"{bank.total_current:.3f}", retain=True)
        self.mqtt.publish(f"{ns}/battery/bank_power", f"{bank.total_power:.1f}", retain=True)

        total_mag = sum(abs(p.current) for p in bank.packs.values())
        for pid, p in bank.packs.items():
            base = f"{ns}/battery/pack{pid}"
            self.mqtt.publish(f"{base}/soc", f"{p.soc:.1f}", retain=True)
            self.mqtt.publish(f"{base}/voltage", f"{p.voltage:.3f}", retain=True)
            self.mqtt.publish(f"{base}/current", f"{p.current:.3f}", retain=True)
            self.mqtt.publish(f"{base}/temperature", f"{p.temperature:.1f}", retain=True)
            share_pct = (abs(p.current) / total_mag * 100.0) if total_mag > 0.1 else 0.0
            self.mqtt.publish(
                f"{ns}/battery/current_share/pack{pid}",
                f"{share_pct:.1f}",
                retain=True,
            )

    def publish_full_charge_state(
        self,
        last_full_charge_ts: int | None,
        days_since: float | None,
    ) -> None:
        ns = self.namespace
        if last_full_charge_ts is not None:
            iso = datetime.fromtimestamp(last_full_charge_ts, tz=timezone.utc).isoformat()
            self.mqtt.publish(f"{ns}/battery/last_full_charge", iso, retain=True)
        if days_since is not None:
            self.mqtt.publish(
                f"{ns}/battery/days_since_full_charge",
                f"{days_since:.2f}",
                retain=True,
            )

    def _publish_gated(self, topic: str, value: float | None, fmt: str = "{:.1f}") -> None:
        """Publish a reading alongside its availability flag.

        Skipping a null and leaving the retained topic untouched is what made
        HA report 178W of AC consumption for 40 hours after the inverter was
        switched off (2026-08-08). A retained value with no live source is
        indistinguishable from a fresh one, so a missing reading has to be
        stated rather than implied by silence.
        """
        avail = f"{topic}/avail"
        if value is None:
            self.mqtt.publish(avail, "offline", retain=True)
            return
        self.mqtt.publish(topic, fmt.format(value), retain=True)
        self.mqtt.publish(avail, "online", retain=True)

    def publish_ac_state(self, ac: "AcState") -> None:
        """Publish AC aggregates.

        The power figures are gated on availability: when the Cerbo reports
        null (inverter off, no shore lead) the entity is marked unavailable
        rather than left holding its last value. State and shore-connected are
        not gated — they are discrete facts that stay true until they change.
        """
        ns = self.namespace
        self._publish_gated(f"{ns}/ac/consumption_power", ac.total_consumption_w)
        self._publish_gated(f"{ns}/ac/input_power", ac.total_input_w)
        self._publish_gated(f"{ns}/ac/output_power", ac.total_output_w)
        if ac.inverter_state_label is not None:
            self.mqtt.publish(f"{ns}/ac/inverter_state", ac.inverter_state_label, retain=True)
        if ac.shore_connected is not None:
            self.mqtt.publish(
                f"{ns}/ac/shore_connected",
                "on" if ac.shore_connected else "off",
                retain=True,
            )

    def publish_solar_state(self, solar: "SolarState") -> None:
        """Publish solar aggregates. Fields the Cerbo hasn't reported are
        skipped rather than sent as 0 — 0W is a real night-time reading and
        must not be confused with 'no controller data'."""
        ns = self.namespace
        pv_power = solar.total_pv_power
        if pv_power is not None:
            self.mqtt.publish(f"{ns}/solar/pv_power", f"{pv_power:.1f}", retain=True)
        primary = solar.primary
        if primary is not None and primary.pv_voltage is not None:
            self.mqtt.publish(f"{ns}/solar/pv_voltage", f"{primary.pv_voltage:.2f}", retain=True)
        amps = solar.total_charge_current
        if amps is not None:
            self.mqtt.publish(f"{ns}/solar/charge_current", f"{amps:.2f}", retain=True)
        y = solar.total_yield_today_kwh
        if y is not None:
            self.mqtt.publish(f"{ns}/solar/yield_today", f"{y:.2f}", retain=True)
        if solar.state_label is not None:
            self.mqtt.publish(f"{ns}/solar/state", solar.state_label, retain=True)
        if primary is not None and primary.error_code is not None:
            self.mqtt.publish(
                f"{ns}/solar/fault",
                "on" if solar.has_error else "off",
                retain=True,
            )

    def publish_availability(self, online: bool) -> None:
        """Collector alive with a fresh Cerbo feed. Every entity's availability
        hangs off this, so a dead feed greys the panel out instead of leaving
        the last retained values looking current."""
        self.mqtt.publish(self.availability_topic,
                          "online" if online else "offline", retain=True, qos=1)

    def publish_cerbo_online(self, online: bool) -> None:
        self.mqtt.publish(
            f"{self.namespace}/cerbo_online",
            "on" if online else "off",
            retain=True,
        )

    def publish_event(self, event: Event) -> None:
        # latest-event topic for HA automations + logbook
        self.mqtt.publish(f"{self.namespace}/events", event.to_json(), retain=False, qos=1)

    # -- BLE-derived discovery + state ---------------------------------------

    def _ble_discovery_entities(self, ns: str) -> list[tuple[str, str, dict]]:
        """HA discovery configs for BLE-derived per-pack signals. Per-cell raw
        voltages stay in SQLite (spec rule); we expose only derived stats."""
        out: list[tuple[str, str, dict]] = []
        # bank-wide cell spread (the cross-pack alarm signal)
        out.append(self._sensor(
            object_id="cell_spread_bank_mv",
            name="Bank Cell Spread",
            state_topic=f"{ns}/ble/cell_spread_bank_mv",
            unit="mV",
            icon="mdi:battery-unknown",
        ))
        for pid in self.pack_ids:
            base = f"{ns}/ble/pack{pid}"
            out.append(self._sensor(
                object_id=f"pack{pid}_cell_spread_mv",
                name=f"Pack {pid} Cell Spread",
                state_topic=f"{base}/cell_spread_mv",
                unit="mV",
                icon="mdi:battery-unknown",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_cell_max_mv",
                name=f"Pack {pid} Cell Max",
                state_topic=f"{base}/cell_max_mv",
                unit="mV",
                icon="mdi:battery-high",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_cell_min_mv",
                name=f"Pack {pid} Cell Min",
                state_topic=f"{base}/cell_min_mv",
                unit="mV",
                icon="mdi:battery-low",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_bms_soc",
                name=f"Pack {pid} BMS SoC",
                state_topic=f"{base}/bms_soc",
                unit="%",
                device_class="battery",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_bms_temperature",
                name=f"Pack {pid} BMS Temperature",
                state_topic=f"{base}/bms_temperature",
                unit="°C",
                device_class="temperature",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_bms_current",
                name=f"Pack {pid} BMS Current",
                state_topic=f"{base}/bms_current",
                unit="A",
                device_class="current",
            ))
            out.append(self._sensor(
                object_id=f"pack{pid}_bms_time_remaining",
                name=f"Pack {pid} BMS Time-Remaining Counter",
                state_topic=f"{base}/bms_time_remaining",
                state_class=None,
                icon="mdi:timer-outline",
            ))
        return out

    def _obd_discovery_entities(self, ns: str) -> list[tuple[str, str, dict]]:
        """HA discovery configs for OBD-II engine data.

        Every one is gated on its own availability topic. With the ignition off
        the adapter answers NO DATA and nothing is stored, so without the gate
        these entities would sit forever showing the RPM from the last drive —
        the same trap the AC power figures have when the inverter is off.
        """
        return [
            self._sensor(
                object_id="engine_rpm",
                name="Engine RPM",
                state_topic=f"{ns}/engine/rpm",
                unit="rpm",
                icon="mdi:engine",
                extra_availability=f"{ns}/engine/rpm/avail",
            ),
            self._sensor(
                object_id="engine_speed_mph",
                name="Vehicle Speed",
                state_topic=f"{ns}/engine/speed_mph",
                unit="mph",
                device_class="speed",
                icon="mdi:speedometer",
                extra_availability=f"{ns}/engine/speed_mph/avail",
            ),
            self._sensor(
                object_id="engine_coolant_c",
                name="Coolant Temperature",
                state_topic=f"{ns}/engine/coolant_c",
                unit="°C",
                device_class="temperature",
                extra_availability=f"{ns}/engine/coolant_c/avail",
            ),
            self._sensor(
                object_id="engine_load_pct",
                name="Engine Load",
                state_topic=f"{ns}/engine/load_pct",
                unit="%",
                icon="mdi:gauge",
                extra_availability=f"{ns}/engine/load_pct/avail",
            ),
            self._binary_sensor(
                object_id="engine_running",
                name="Engine Running",
                state_topic=f"{ns}/engine/running",
                device_class="running",
            ),
        ]

    def _cabin_discovery_entities(self, ns: str) -> list[tuple[str, str, dict]]:
        """HA discovery configs for the cabin sensors, gated like the engine.

        The sensor sits in the electronics cabinet, so this is the packs'
        ambient. Out of BLE range with the cloud poll failing, the table just
        stops; without the gate HA would hold the last temperature forever.
        Published in F per the ambient-in-F convention — HA converts for
        display if its unit system says otherwise.
        """
        out: list[tuple[str, str, dict]] = []
        for sensor in self.cabin_sensors:
            base = f"{ns}/cabin/{sensor}"
            label = sensor.replace("_", " ").title()
            out.append(self._sensor(
                object_id=f"{sensor}_temperature",
                name=f"{label} Temperature",
                state_topic=f"{base}/temperature",
                unit="°F",
                device_class="temperature",
                extra_availability=f"{base}/temperature/avail",
            ))
            out.append(self._sensor(
                object_id=f"{sensor}_humidity",
                name=f"{label} Humidity",
                state_topic=f"{base}/humidity",
                unit="%",
                device_class="humidity",
                extra_availability=f"{base}/humidity/avail",
            ))
        return out

    def publish_cabin_state(self, samples: "dict[str, CabinSample]") -> None:
        """Publish each configured cabin sensor, or mark it unavailable.

        A configured sensor with no fresh row is published as offline rather
        than skipped — skipping is exactly how a retained topic goes stale.
        """
        ns = self.namespace
        for sensor in self.cabin_sensors:
            s = samples.get(sensor)
            base = f"{ns}/cabin/{sensor}"
            self._publish_gated(f"{base}/temperature", s.air_temp_f if s else None)
            self._publish_gated(f"{base}/humidity", s.humidity_pct if s else None)

    def publish_engine_state(self, sample: EngineSample) -> None:
        """Publish engine data read back out of SQLite by the collector.

        The OBD reader lives in the BLE sidecar and does not speak MQTT, so
        this mirrors publish_ble_state: sidecar writes the table, collector
        publishes it.
        """
        ns = self.namespace
        self._publish_gated(f"{ns}/engine/rpm", sample.rpm)
        self._publish_gated(f"{ns}/engine/speed_mph", sample.speed_mph)
        self._publish_gated(f"{ns}/engine/coolant_c", sample.coolant_c)
        self._publish_gated(f"{ns}/engine/load_pct", sample.engine_load_pct)
        # Ungated and RPM-derived: "is the alternator turning" is the single
        # fact this whole module exists to record, and it stays true until it
        # changes. runtime_s is deliberately not used — some ECUs hold it
        # briefly after shutdown, and the exact moment charging stops is the
        # point.
        if sample.rpm is not None:
            self.mqtt.publish(
                f"{ns}/engine/running",
                "on" if sample.rpm > 0 else "off",
                retain=True,
            )

    def publish_ble_state(
        self,
        samples_by_pack: "dict[int, BlePackSample]",
    ) -> None:
        """Publish derived BLE stats per pack. Raw per-cell voltages stay in
        SQLite — only spread/min/max/SoC/etc. go to MQTT."""
        if not samples_by_pack:
            return
        ns = self.namespace
        all_cells: list[int] = []
        for pid, s in samples_by_pack.items():
            if s.cells_mv:
                all_cells.extend(s.cells_mv)
            base = f"{ns}/ble/pack{pid}"
            if s.cells_mv:
                self.mqtt.publish(f"{base}/cell_spread_mv",
                                  str(max(s.cells_mv) - min(s.cells_mv)), retain=True)
                self.mqtt.publish(f"{base}/cell_max_mv", str(max(s.cells_mv)), retain=True)
                self.mqtt.publish(f"{base}/cell_min_mv", str(min(s.cells_mv)), retain=True)
            if s.soc_pct is not None:
                self.mqtt.publish(f"{base}/bms_soc", str(s.soc_pct), retain=True)
            if s.temp_dc is not None:
                self.mqtt.publish(f"{base}/bms_temperature",
                                  f"{s.temp_dc/10:.1f}", retain=True)
            if s.current_a is not None:
                self.mqtt.publish(f"{base}/bms_current", str(s.current_a), retain=True)
            if s.big_counter is not None:
                self.mqtt.publish(f"{base}/bms_time_remaining",
                                  str(s.big_counter), retain=True)
        if all_cells:
            self.mqtt.publish(f"{ns}/ble/cell_spread_bank_mv",
                              str(max(all_cells) - min(all_cells)), retain=True)
