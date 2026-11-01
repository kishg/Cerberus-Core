from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from . import detectors as detectors_mod
from . import drives as drives_mod
from . import envfile as envfile_mod
from . import geocode as geocode_mod
from . import heater as heater_mod
from . import location as location_mod
from . import model as model_mod
from . import obd as obd_mod
from . import profile as profile_mod
from . import solar_path as solar_path_mod


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class MqttConfig:
    host: str                       # primary host (== hosts[0]) — kept for logging/compat
    hosts: tuple[str, ...]          # ordered failover list
    port: int
    portal_id: str
    username: str
    password: str
    keepalive_publish_seconds: int
    primary_topic: str
    publish_namespace: str
    ha_discovery_prefix: str
    # How often a periodic keepalive may ask the GX for a full republish. The
    # other keepalives carry suppress-republish. See CerboMqtt._keepalive_loop.
    full_republish_seconds: int = 300
    # VRM cloud MQTT relay as a last resort (cerberus.vrm_cloud). Started only
    # after the local feed has been down cloud_after_seconds, and only when
    # VRM_TOKEN is set; stopped the moment the local link is back.
    cloud_fallback: bool = False
    cloud_after_seconds: int = 180


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: str
    sample_downsample_seconds: int


@dataclass(frozen=True)
class SyncConfig:
    enabled: bool
    target_url: str
    auth_token: str
    batch_size: int
    interval_seconds: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class BleConfig:
    enabled: bool
    pack_addresses: Mapping[int, str]
    cabin_sensors: Mapping[str, str]
    cabin_sample_seconds: int
    sample_downsample_seconds: int
    reconnect_backoff_seconds: int
    # Bounds BleakClient.connect(), which on macOS can wait forever on an
    # address CoreBluetooth cannot resolve — bleak's own timeout does not cover
    # it. Default 60 s from the log: out-of-range failures land at exactly
    # 30.0 s, but a *successful* pack connect has taken 33.3 s (2026-09-21
    # 14:35:42 -> 14:36:15), so a successful connect can outlast bleak's own
    # discovery timeout. Anything at or below 30 s would have killed that one.
    connect_timeout_seconds: float = 60.0
    # Lithionics ACR (alternator charge relay). Empty = not collected. Same
    # FFE0/FFE1 transport as the packs, but it holds a fourth connection.
    acr_address: str = ""
    # A frame is written whenever an unmapped field changes (contactor events
    # land at the ACR's ~1 s resolution), else at most this often.
    acr_sample_seconds: int = 30


@dataclass(frozen=True)
class ObdConfig:
    enabled: bool
    address: str
    write_char: str
    notify_char: str
    poll_seconds: int
    # Two separate command timeouts because ATZ (full adapter reset) is by far
    # the slowest command in the sequence and a per-PID timeout long enough for
    # it would stall the whole poll cycle whenever the ignition is off.
    command_timeout_seconds: float
    init_timeout_seconds: float
    # Bounds the CONNECT, which is a separate problem from either. Verified
    # 2026-09-30: BleakClient.connect() against an address CoreBluetooth cannot
    # resolve never returns on macOS — it does not honour bleak's own discovery
    # timeout — so without an explicit bound a shutdown blocks forever waiting
    # on a connect that will never finish. Shares BleConfig's 60 s default and
    # its reasoning: the 33.3 s successful connect that sets the floor is a
    # property of this CoreBluetooth stack, not of the pack radios.
    connect_timeout_seconds: float
    reconnect_backoff_seconds: int
    # Engine-off behaviour. The adapter lives on the chassis battery's OBD
    # port and stays plugged in, so a parked van must not be kept awake by it.
    # Once this many consecutive poll cycles come back empty the sidecar
    # disconnects (letting the adapter sleep) and only reconnects every
    # parked_probe_seconds, asking for ATRV rather than a PID — the adapter
    # answers that itself, with no traffic on the vehicle bus. Polling resumes
    # when ATRV reads at least running_volts, i.e. the alternator is charging.
    parked_after_empty_polls: int = 3
    parked_probe_seconds: int = 300
    running_volts: float = 13.3
    # Manufacturer-specific reads (Mercedes UDS service 22) beyond the standard
    # PIDs, one obd_mod.RequestSpec per obd.extra entry. See config.yaml.
    extra: tuple = ()


def _load_obd_extra(entries) -> tuple:
    """Parse obd.extra into RequestSpecs, failing loudly at startup.

    These are hand-entered from sniffing sessions, so typos are expected: a
    bad formula, unknown tier or duplicate name raises here instead of quietly
    logging None for every drive.
    """
    if not isinstance(entries, list):
        raise ConfigError("obd.extra must be a list")
    specs = []
    seen = {s.name for s in obd_mod.STANDARD_CATALOG}
    for i, e in enumerate(entries):
        where = f"obd.extra[{i}]"
        if not isinstance(e, dict):
            raise ConfigError(f"{where} must be a mapping")
        missing = [k for k in ("name", "request", "unit", "formula") if k not in e]
        if missing:
            raise ConfigError(f"{where} is missing {', '.join(missing)}")
        name = str(e["name"]).strip()
        if name in seen:
            raise ConfigError(f"{where}: name {name!r} is already used")
        seen.add(name)
        request = str(e["request"]).strip().upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2})+", request):
            raise ConfigError(f"{where}: request {request!r} must be hex bytes, e.g. 22F190")
        tier = str(e.get("tier", "medium"))
        if tier not in obd_mod.TIERS:
            raise ConfigError(f"{where}: tier must be one of {', '.join(obd_mod.TIERS)}")
        header = e.get("header")
        receive = e.get("receive")
        for key, val in (("header", header), ("receive", receive)):
            if val is not None and not re.fullmatch(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8}", str(val)):
                raise ConfigError(f"{where}: {key} {val!r} must be a 3-, 6- or 8-digit hex CAN ID")
        try:
            obd_mod.compile_formula(str(e["formula"]))
        except obd_mod.FormulaError as err:
            raise ConfigError(f"{where}: {err}") from None
        specs.append(obd_mod.RequestSpec(
            name=name, request=request, unit=str(e["unit"]),
            formula=str(e["formula"]), tier=tier,
            header=None if header is None else str(header).upper(),
            receive=None if receive is None else str(receive).upper(),
            min_value=None if e.get("min") is None else float(e["min"]),
            max_value=None if e.get("max") is None else float(e["max"]),
        ))
    return tuple(specs)


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool
    latitude: float
    longitude: float
    user_agent: str
    interval_seconds: int
    request_timeout_seconds: int
    # The home ZIP the latitude/longitude were looked up from (cerberus.geocode);
    # informational, the position is what weather uses.
    zip_code: str | None = None


@dataclass(frozen=True)
class GoveeCloudConfig:
    enabled: bool
    # Which cabin_samples.sensor name the cloud rows land under. Same name as
    # the BLE mapping so both sources describe one sensor; `source` tells
    # them apart.
    sensor: str
    sku: str
    device: str
    interval_seconds: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class DrivesConfig:
    enabled: bool
    # Where per-drive markdown reports go. Relative paths resolve against the
    # database's directory, so the soak layout puts them in soak/data/reports.
    report_dir: str
    # YYYY-MM-DD dates of hardware changes; see drives.DEFAULT_EPOCHS.
    baseline_epochs: tuple[str, ...]
    # Joint-probe instance and limits; see drives.Settings.
    settings: drives_mod.Settings = drives_mod.DEFAULT_SETTINGS


@dataclass(frozen=True)
class HaMqttConfig:
    """Home Assistant's own broker, where Cerberus publishes state, events and
    discovery (cerberus.ha_mqtt). Not the Cerbo.

    Optional: the Cerberus UI is the product's UI, so a new install leaves it
    off; this van, which runs HA, turns it on in config.yaml."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883


@dataclass(frozen=True)
class BankConfig:
    """Where bank figures come from; see model.BankState.source."""
    source: str = model_mod.SOURCE_PACKS
    # SOURCE_MONITOR only: the monitor's battery instance. None = whichever
    # service the GX marks active_battery_service.
    monitor_instance: int | None = None
    # Rated bank capacity, for when the BMS / monitor does not report one.
    capacity_ah: float | None = None
    # Derived, not set: battery_samples rows per whole-bank sample.
    pack_count: int = 1


@dataclass(frozen=True)
class SolarArrayConfig:
    """The PV array, which sizes the solar-open thresholds; see
    detectors.solar_open_thresholds."""
    # Open-circuit voltage of the whole array as measured in full sun:
    # one panel's for parallel wiring, times the panel count in series.
    array_voc_v: float = detectors_mod.ARRAY_VOC_V
    array_w: float = detectors_mod.ARRAY_W


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    storage: StorageConfig
    sync: SyncConfig
    packs: Mapping[str, int]
    thresholds: Mapping[str, float]
    full_charge: Mapping[str, float]
    ble: BleConfig
    weather: WeatherConfig
    obd: ObdConfig
    govee_cloud: GoveeCloudConfig
    drives: DrivesConfig
    ha_mqtt: HaMqttConfig = HaMqttConfig()
    solar_path: solar_path_mod.Settings = solar_path_mod.DEFAULT_SETTINGS
    heater: heater_mod.Settings = heater_mod.DEFAULT_SETTINGS
    bank: BankConfig = BankConfig()
    system: profile_mod.VoltageProfile = profile_mod.DEFAULT_PROFILE
    solar: SolarArrayConfig = SolarArrayConfig()
    # IANA zone overriding the GX's own setting; see location.VanLocation.
    timezone: str | None = None


def _parse_timezone(raw: dict) -> str | None:
    name = (raw.get("system") or {}).get("timezone")
    if name is None or str(name).strip() == "":
        return None
    name = str(name).strip()
    try:
        location_mod.ZoneInfo(name)
    except (location_mod.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"system.timezone: unknown IANA zone {name!r}") from None
    return name


def _parse_solar_array(raw: dict) -> SolarArrayConfig:
    so = raw.get("solar") or {}
    try:
        arr = SolarArrayConfig(
            array_voc_v=float(so.get("array_voc_v", detectors_mod.ARRAY_VOC_V)),
            array_w=float(so.get("array_w", detectors_mod.ARRAY_W)),
        )
    except (TypeError, ValueError) as err:
        raise ConfigError(f"solar: {err}") from None
    if arr.array_voc_v <= 0 or arr.array_w <= 0:
        raise ConfigError("solar: array_voc_v and array_w must be positive")
    return arr


def _parse_system(raw: dict) -> profile_mod.VoltageProfile:
    sy = raw.get("system") or {}
    try:
        return profile_mod.profile(
            nominal_v=int(sy.get("nominal_voltage", 12)),
            chemistry=str(sy.get("chemistry", profile_mod.LIFEPO4)).strip().lower(),
        )
    except (TypeError, ValueError) as err:
        raise ConfigError(f"system: {err}") from None


# met.no asks for an identifying User-Agent with contact information; a
# project website qualifies. An install that fetches a lot should set its own.
DEFAULT_WEATHER_UA = "Cerberus/0.1 (van electrical monitor; +https://github.com/kishg/Cerberus-Core)"


def _parse_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("true", "yes", "on", "1"):
        return True
    if v in ("false", "no", "off", "0", ""):
        return False
    raise ConfigError(f"{name}: expected true or false (got {value!r})")


def _parse_zip(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return geocode_mod.normalize_zip(value)
    except geocode_mod.GeocodeError as err:
        raise ConfigError(f"weather.zip_code: {err}") from None


def _parse_packs(raw: dict) -> dict[str, int]:
    """uc token -> pack id; ids positive and one per pack."""
    pk = raw.get("packs") or {}
    if not isinstance(pk, dict):
        raise ConfigError("packs must map uc<NNN> tokens to pack ids")
    try:
        packs = {str(k): int(v) for k, v in pk.items()}
    except (TypeError, ValueError):
        raise ConfigError("packs: every pack id must be an integer") from None
    if any(v < 1 for v in packs.values()) or len(set(packs.values())) != len(packs):
        raise ConfigError("packs: pack ids must be positive and different")
    return packs


def _parse_bank(raw: dict) -> BankConfig:
    b = raw.get("bank") or {}
    source = str(b.get("source", model_mod.SOURCE_PACKS))
    if source not in model_mod.BANK_SOURCES:
        raise ConfigError(
            f"bank.source must be one of {', '.join(model_mod.BANK_SOURCES)} (got {source!r})"
        )
    instance = b.get("monitor_instance")
    try:
        instance = None if instance is None else int(instance)
    except (TypeError, ValueError):
        raise ConfigError(f"bank.monitor_instance: {instance!r} is not an integer") from None
    cap = b.get("capacity_ah")
    try:
        cap = None if cap is None else float(cap)
    except (TypeError, ValueError):
        raise ConfigError(f"bank.capacity_ah: {cap!r} is not a number") from None
    if cap is not None and cap <= 0:
        raise ConfigError(f"bank.capacity_ah must be positive (got {cap})")
    pack_ids = set((raw.get("packs") or {}).values())
    return BankConfig(
        source=source, monitor_instance=instance, capacity_ah=cap,
        pack_count=1 if source == model_mod.SOURCE_MONITOR else max(1, len(pack_ids)),
    )


def _parse_drives(raw: dict, prof: profile_mod.VoltageProfile,
                  bank: BankConfig) -> DrivesConfig:
    d_raw = raw.get("drives") or {}
    instance = d_raw.get("joint_temp_instance")
    try:
        settings = drives_mod.Settings(
            joint_temp_instance=None if instance is None else int(instance),
            joint_limit_c=float(d_raw.get("joint_limit_c", drives_mod.ABS_JOINT_C)),
            joint_cold_c=float(d_raw.get("joint_cold_c", drives_mod.JOINT_COLD_C)),
            peak_limit_v=prof.peak_limit_v,
            over_request_v=prof.over_request_v,
            bulk_max_v=prof.bulk_max_v,
            pack_count=bank.pack_count,
            engine_charge_a=float(d_raw.get("engine_charge_a", drives_mod.ENGINE_CHARGE_A)),
            capacity_ah=bank.capacity_ah,
        )
    except (TypeError, ValueError) as err:
        raise ConfigError(f"drives: {err}") from None
    if settings.engine_charge_a <= 0:
        raise ConfigError(
            f"drives.engine_charge_a must be positive (got {settings.engine_charge_a})")
    drives = DrivesConfig(
        enabled=bool(d_raw.get("enabled", True)),
        report_dir=str(d_raw.get("report_dir", "reports")),
        baseline_epochs=tuple(str(e) for e in (d_raw.get("baseline_epochs") or [])),
        settings=settings,
    )
    for e in drives.baseline_epochs:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e):
            raise ConfigError(f"drives.baseline_epochs: {e!r} is not YYYY-MM-DD")
    return drives


def _parse_solar_path(raw: dict, prof: profile_mod.VoltageProfile,
                      drives: DrivesConfig) -> solar_path_mod.Settings:
    sp = raw.get("solar_path") or {}
    try:
        settings = solar_path_mod.Settings(
            absorption_v=float(sp.get("absorption_v", prof.absorption_v)),
            path_limit_mohm=float(sp.get("path_limit_mohm", solar_path_mod.ABS_PATH_MOHM)),
            process_after_hour=int(sp.get("process_after_hour",
                                          solar_path_mod.PROCESS_AFTER_HOUR)),
            pack_count=drives.settings.pack_count,
            charge_on_a=drives.settings.charge_on_a,
        )
    except (TypeError, ValueError) as err:
        raise ConfigError(f"solar_path: {err}") from None
    if not 0 <= settings.process_after_hour <= 23:
        raise ConfigError(
            f"solar_path.process_after_hour must be 0-23 (got {settings.process_after_hour})"
        )
    return settings


def _parse_heater(raw: dict, bank: BankConfig) -> heater_mod.Settings:
    h = raw.get("heater") or {}
    try:
        settings = heater_mod.Settings(
            on_delta_w=float(h.get("on_delta_w", heater_mod.ON_DELTA_W)),
            off_delta_w=float(h.get("off_delta_w", heater_mod.OFF_DELTA_W)),
            baseline_max_w=float(h.get("baseline_max_w", heater_mod.BASELINE_MAX_W)),
            default_baseline_w=float(h.get("default_baseline_w",
                                           heater_mod.DEFAULT_BASELINE_W)),
            pack_count=bank.pack_count,
        )
    except (TypeError, ValueError) as err:
        raise ConfigError(f"heater: {err}") from None
    # Off below on, or a run would end on the sample that started it.
    if not 0 < settings.off_delta_w < settings.on_delta_w:
        raise ConfigError("heater: need 0 < off_delta_w < on_delta_w")
    return settings


@dataclass(frozen=True)
class UiConfig:
    """What the UI process needs (cerberus.ui): where to listen, the database,
    and the few analysis values its views use."""
    host: str = "0.0.0.0"
    port: int = 8088
    sqlite_path: str = ""
    pack_count: int = 1
    capacity_ah: float | None = None
    timezone: str | None = None
    path_limit_mohm: float = 75.0
    full_remind_days: float = 30.0
    # Engine current that counts as charging (drives.Settings.charge_on_a): the
    # UI's "is the van moving" test for phone positions (cerberus.phonefix).
    charge_on_a: float = 20.0
    # Where the UI's footer points for its source code (AGPL-3.0 section 13).
    source_url: str = "https://github.com/kishg/Cerberus-Core"


# Settings changed in the UI land here, beside config.yaml, and win over it.
# config.yaml stays hand-edited: rewriting it through a YAML library would
# drop every comment in it. See cerberus.settings.
SETUP_FILE = "setup.yaml"


def setup_path(config_path: str | Path) -> Path:
    return Path(config_path).with_name(SETUP_FILE)


# Top-level mappings setup.yaml replaces whole rather than merging into: a
# detected pack map must not keep a pack config.yaml lists but the van no
# longer has.
# Dotted paths. Likewise a scanned set of Bluetooth devices replaces the
# configured one.
REPLACED_KEYS = ("packs", "ble.pack_addresses", "ble.cabin_sensors")


def merge(base: dict, over: dict, _path: str = "") -> dict:
    """Deep merge: mappings merge key by key, anything else in `over` wins
    (and so do the REPLACED_KEYS mappings, whole)."""
    out = dict(base)
    for k, v in (over or {}).items():
        path = f"{_path}{k}"
        if (isinstance(v, dict) and isinstance(out.get(k), dict)
                and path not in REPLACED_KEYS):
            out[k] = merge(out[k], v, _path=path + ".")
        else:
            out[k] = v
    return out


def read_raw(path: str | Path) -> dict:
    """config.yaml with setup.yaml merged over it. A box not set up yet has
    no config.yaml; that reads as empty (see is_set_up)."""
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raw = {}
    sp = setup_path(path)
    if sp.exists():
        over = yaml.safe_load(sp.read_text()) or {}
        if not isinstance(over, dict):
            raise ConfigError(f"{sp}: must be a mapping")
        raw = merge(raw, over)
    return raw


def load_ui(path: str | Path) -> UiConfig:
    """Like load_analysis, needs no MQTT password: the UI never talks to MQTT."""
    raw = read_raw(path)
    u = raw.get("ui") or {}
    bank = _parse_bank(raw)
    prof = _parse_system(raw)
    drives = _parse_drives(raw, prof, bank)
    solar_path = _parse_solar_path(raw, prof, drives)
    try:
        port = int(u.get("port", 8088))
    except (TypeError, ValueError):
        raise ConfigError(f"ui.port: {u.get('port')!r} is not an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"ui.port must be 1-65535 (got {port})")
    return UiConfig(
        # 0.0.0.0: reachable from a phone on the van LAN, and over Tailscale.
        host=str(u.get("host", "0.0.0.0")),
        port=port,
        sqlite_path=str((raw.get("storage") or {}).get("sqlite_path", "")),
        pack_count=bank.pack_count,
        capacity_ah=bank.capacity_ah,
        timezone=_parse_timezone(raw),
        path_limit_mohm=solar_path.path_limit_mohm,
        full_remind_days=float((raw.get("full_charge") or {}).get("remind_after_days", 30)),
        source_url=str(u.get("source_url", UiConfig.source_url)),
        charge_on_a=drives.settings.charge_on_a,
    )


def load_analysis(path: str | Path) -> tuple[DrivesConfig, solar_path_mod.Settings, str | None]:
    """The drive and solar-path settings and the configured time zone alone,
    for tools that rebuild reports offline. Unlike load(), needs no MQTT
    password."""
    raw = read_raw(path)
    prof = _parse_system(raw)
    drives = _parse_drives(raw, prof, _parse_bank(raw))
    return drives, _parse_solar_path(raw, prof, drives), _parse_timezone(raw)


NOT_SET_UP = "not set up yet: no Cerbo connection (mqtt.portal_id); run setup in the UI"


def is_set_up(path: str | Path) -> bool:
    """Whether this box knows its Cerbo (first-run setup has happened)."""
    try:
        raw = read_raw(path)
    except (ConfigError, yaml.YAMLError):
        return True          # a config exists, it just doesn't load: not a first run
    return bool((raw.get("mqtt") or {}).get("portal_id"))


def load(path: str | Path) -> Config:
    return from_raw(read_raw(path), password=envfile_mod.mqtt_password(path))


def from_raw(raw: dict, password: str | None = None) -> Config:
    """Build and validate a Config from merged YAML. The password comes from
    the environment unless given (validation passes a placeholder)."""
    if not (raw.get("mqtt") or {}).get("portal_id"):
        raise ConfigError(NOT_SET_UP)
    password = password or os.environ.get("CERBERUS_MQTT_PASSWORD", "")
    if not password:
        raise ConfigError(
            "CERBERUS_MQTT_PASSWORD env var is required (Cerbo Remote Console password)."
        )

    m = raw["mqtt"]
    # Failover list. Env override wins (CSV); else yaml mqtt.hosts list; else
    # the legacy single mqtt.host string. Hosts are stable identifiers
    # (hostnames, not IPs) so DHCP-renewal doesn't break the binding.
    env_host = os.environ.get("CERBERUS_MQTT_HOST")
    if env_host:
        hosts_raw = [h.strip() for h in env_host.split(",") if h.strip()]
    elif m.get("hosts"):
        hosts_raw = [str(h).strip() for h in m["hosts"] if str(h).strip()]
    elif m.get("host"):
        hosts_raw = [str(m["host"]).strip()]
    else:
        hosts_raw = []
    if not hosts_raw:
        raise ConfigError(
            "MQTT host not set — provide mqtt.host or mqtt.hosts in config.yaml, "
            "or CERBERUS_MQTT_HOST env (comma-separated for multi-host)."
        )
    hosts = tuple(hosts_raw)
    mqtt = MqttConfig(
        host=hosts[0],
        hosts=hosts,
        port=int(m.get("port", 1883)),
        portal_id=m["portal_id"],
        username=m.get("username", m["portal_id"]),
        password=password,
        keepalive_publish_seconds=int(m.get("keepalive_publish_seconds", 30)),
        primary_topic=m.get("primary_topic", "system/0/Batteries"),
        publish_namespace=m.get("publish_namespace", "cerberus"),
        ha_discovery_prefix=m.get("ha_discovery_prefix", "homeassistant"),
        full_republish_seconds=int(m.get("full_republish_seconds", 300)),
        cloud_fallback=bool(m.get("cloud_fallback", False)),
        cloud_after_seconds=int(m.get("cloud_after_seconds", 180)),
    )
    s = raw.get("storage") or {}
    if not (os.environ.get("CERBERUS_SQLITE_PATH") or s.get("sqlite_path")):
        raise ConfigError("storage.sqlite_path is not set")
    storage = StorageConfig(
        sqlite_path=os.environ.get("CERBERUS_SQLITE_PATH") or str(s["sqlite_path"]),
        sample_downsample_seconds=int(s.get("sample_downsample_seconds", 30)),
    )
    sync_raw = raw.get("sync") or {}
    sync = SyncConfig(
        enabled=bool(sync_raw.get("enabled", False)),
        target_url=sync_raw.get("target_url", ""),
        auth_token=os.environ.get("CERBERUS_SYNC_TOKEN") or sync_raw.get("auth_token", ""),
        batch_size=int(sync_raw.get("batch_size", 500)),
        interval_seconds=int(sync_raw.get("interval_seconds", 60)),
        request_timeout_seconds=int(sync_raw.get("request_timeout_seconds", 10)),
    )
    if sync.enabled and not sync.target_url:
        raise ConfigError("sync.enabled is true but sync.target_url is empty.")

    bank = _parse_bank(raw)
    packs = _parse_packs(raw)
    if not packs and bank.source == model_mod.SOURCE_PACKS:
        raise ConfigError(
            "packs map is empty — set uc<NNN> -> pack_id in config.yaml, or "
            "bank.source: monitor for a bank measured by one battery monitor"
        )

    ble_raw = raw.get("ble") or {}
    ble_addrs_raw = ble_raw.get("pack_addresses") or {}
    ble_addrs: dict[int, str] = {}
    for k, v in ble_addrs_raw.items():
        try:
            ble_addrs[int(k)] = str(v)
        except (TypeError, ValueError):
            raise ConfigError(f"ble.pack_addresses: bad pack id {k!r}")
    w_raw = raw.get("weather") or {}
    weather = WeatherConfig(
        # On by default: it costs one request per 15 min, and a new van has
        # nothing else telling it the outside temperature. It waits for a
        # position (GPS, phone, ZIP or latitude/longitude) before fetching.
        enabled=bool(w_raw.get("enabled", True)),
        latitude=float(w_raw.get("latitude", 0.0)),
        longitude=float(w_raw.get("longitude", 0.0)),
        # met.no rejects requests without an identifying User-Agent and asks
        # for contact information. Until 5.6 this was required with no
        # default, so one install's misuse couldn't get a shared one blocked;
        # a new van now gets DEFAULT_WEATHER_UA (the project's site as the
        # contact) so weather works out of the box. Fine for a handful of
        # beta vans; a fleet should give each install its own.
user_agent=str(w_raw.get("user_agent", DEFAULT_WEATHER_UA)).strip(),
        interval_seconds=int(w_raw.get("interval_seconds", 900)),
        request_timeout_seconds=int(w_raw.get("request_timeout_seconds", 10)),
        zip_code=_parse_zip(w_raw.get("zip_code")),
    )
    if weather.enabled:
        if not weather.user_agent:
            raise ConfigError(
                "weather.enabled is true but weather.user_agent is empty — "
                "met.no requires an identifying User-Agent with contact info."
            )
        if not (-90 <= weather.latitude <= 90) or not (-180 <= weather.longitude <= 180):
            raise ConfigError(
                f"weather lat/lon out of range: {weather.latitude}, {weather.longitude}"
            )

    cabin_raw = ble_raw.get("cabin_sensors") or {}
    if not isinstance(cabin_raw, dict) or any(not str(v).strip() for v in cabin_raw.values()):
        raise ConfigError("ble.cabin_sensors: every sensor needs a name (cabin, fridge, ...)")
    ble = BleConfig(
        cabin_sensors={str(k): str(v) for k, v in cabin_raw.items()},
        cabin_sample_seconds=int(ble_raw.get("cabin_sample_seconds", 60)),
        enabled=bool(ble_raw.get("enabled", False)),
        pack_addresses=ble_addrs,
        sample_downsample_seconds=int(
            ble_raw.get("sample_downsample_seconds",
                        storage.sample_downsample_seconds)
        ),
        reconnect_backoff_seconds=int(ble_raw.get("reconnect_backoff_seconds", 5)),
        connect_timeout_seconds=float(ble_raw.get("connect_timeout_seconds", 60.0)),
        acr_address=str(ble_raw.get("acr_address") or "").strip(),
        acr_sample_seconds=int(ble_raw.get("acr_sample_seconds", 30)),
    )
    # At least one device: packs, the ACR, or a temperature sensor alone (a
    # van whose batteries aren't Lithionics can still log its cabin).
    if ble.enabled and not (ble.pack_addresses or ble.acr_address or ble.cabin_sensors):
        raise ConfigError("ble.enabled is true but no Bluetooth device is set; "
                          "use Settings > Find Bluetooth devices")

    obd_raw = raw.get("obd") or {}
    obd = ObdConfig(
        enabled=bool(obd_raw.get("enabled", False)),
        address=str(obd_raw.get("address", "")).strip(),
        # Defaulted to the Nordic UART Service, which is the common BLE-serial
        # profile — but only a guess for any given adapter, so it is
        # overridable. The packs on this rig use FFE0/FFE1 instead.
        write_char=str(obd_raw.get("write_char", obd_mod.DEFAULT_WRITE_CHAR)).strip(),
        notify_char=str(obd_raw.get("notify_char", obd_mod.DEFAULT_NOTIFY_CHAR)).strip(),
        # 2 s is deliberate. RPM is the signal and it moves fast; sampling it
        # slower than the 30 s electrical cadence would defeat the purpose of
        # correlating the two.
        poll_seconds=int(obd_raw.get("poll_seconds", 2)),
        command_timeout_seconds=float(obd_raw.get("command_timeout_seconds", 1.0)),
        init_timeout_seconds=float(obd_raw.get("init_timeout_seconds", 10.0)),
        connect_timeout_seconds=float(obd_raw.get("connect_timeout_seconds", 60.0)),
        reconnect_backoff_seconds=int(obd_raw.get("reconnect_backoff_seconds", 5)),
        parked_after_empty_polls=int(obd_raw.get("parked_after_empty_polls", 3)),
        parked_probe_seconds=int(obd_raw.get("parked_probe_seconds", 300)),
        running_volts=float(obd_raw.get("running_volts", 13.3)),
        extra=_load_obd_extra(obd_raw.get("extra") or []),
    )
    if obd.enabled:
        if not obd.address:
            raise ConfigError(
                "obd.enabled is true but obd.address is empty — set the adapter's "
                "CoreBluetooth UUID (macOS) or MAC, found with "
                "`python tools/ble_capture.py scan`."
            )
        if obd.poll_seconds < 1:
            raise ConfigError(f"obd.poll_seconds must be >= 1 (got {obd.poll_seconds})")
        if obd.parked_after_empty_polls < 1:
            raise ConfigError(
                f"obd.parked_after_empty_polls must be >= 1 (got {obd.parked_after_empty_polls})"
            )
        if obd.parked_probe_seconds < 30:
            raise ConfigError(
                f"obd.parked_probe_seconds must be >= 30 (got {obd.parked_probe_seconds}) — "
                "anything shorter keeps the adapter awake on the chassis battery"
            )
        # The reader runs inside the BLE sidecar, sharing its event loop and
        # CoreBluetooth connection slots. With ble disabled that process does
        # not start at all, so the adapter would never be polled and the
        # failure would be silent.
        if not ble.enabled:
            raise ConfigError(
                "obd.enabled is true but ble.enabled is false — the OBD reader "
                "runs inside the BLE sidecar, which will not start."
            )

    g_raw = raw.get("govee_cloud") or {}
    govee_cloud = GoveeCloudConfig(
        enabled=bool(g_raw.get("enabled", False)),
        sensor=str(g_raw.get("sensor", "cabin")),
        sku=str(g_raw.get("sku", "")).strip(),
        device=str(g_raw.get("device", "")).strip(),
        interval_seconds=int(g_raw.get("interval_seconds", 300)),
        request_timeout_seconds=int(g_raw.get("request_timeout_seconds", 10)),
    )
    if govee_cloud.enabled and not (govee_cloud.sku and govee_cloud.device):
        raise ConfigError("govee_cloud.enabled is true but sku/device are unset.")
    # The API key is NOT checked here: it lives in .env, and a missing key
    # should cost the cabin fallback, not stop the collector from starting.

    system = _parse_system(raw)
    drives = _parse_drives(raw, system, bank)
    # Bank voltages the config leaves unset come from the profile, so a 24 V
    # bank does not inherit 12 V alarm levels.
    thresholds = dict(raw.get("thresholds") or {})
    thresholds.setdefault("low_voltage_crit_v", system.crit_v)
    # Likewise the solar-open thresholds come from the array, and its
    # "another charger is on" guard is the same one the drive report uses.
    solar = _parse_solar_array(raw)
    for key, value in detectors_mod.solar_open_thresholds(
            solar.array_voc_v, solar.array_w).items():
        thresholds.setdefault(key, value)
    thresholds.setdefault("solar_open_charging_guard_a", drives.settings.charge_on_a)
    full_charge = dict(raw.get("full_charge") or {})
    full_charge.setdefault("absorption_voltage_min", system.full_min_v)

    return Config(
        mqtt=mqtt,
        storage=storage,
        sync=sync,
        packs=packs,
        thresholds=thresholds,
        full_charge=full_charge,
        ble=ble,
        weather=weather,
        obd=obd,
        govee_cloud=govee_cloud,
        drives=drives,
        ha_mqtt=HaMqttConfig(
            enabled=_parse_bool((raw.get("ha_mqtt") or {}).get("enabled", False), "ha_mqtt.enabled"),
            host=str((raw.get("ha_mqtt") or {}).get("host", "127.0.0.1")),
            port=int((raw.get("ha_mqtt") or {}).get("port", 1883)),
        ),
        solar_path=_parse_solar_path(raw, system, drives),
        heater=_parse_heater(raw, bank),
        bank=bank,
        system=system,
        solar=solar,
        timezone=_parse_timezone(raw),
    )
