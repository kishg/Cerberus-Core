"""Owner-editable settings: what the UI's Settings screen shows and writes.

Writes go to setup.yaml beside config.yaml (config.read_raw merges it over),
never to config.yaml, whose comments a YAML round trip would destroy. Every
value shows where it came from: set in the UI ("setup"), in config.yaml
("config"), or the built-in default.

apply() never writes a file that would not load: it merges the change into a
copy, validates the whole result with the same loader the collector uses,
and only then replaces setup.yaml atomically. The collector notices the
change and restarts into it (see Cerberus._config_changed).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config as cfg_mod
from . import detect as detect_mod
from . import geocode as geocode_mod
from . import learn as learn_mod
from . import model as model_mod
from . import profile as profile_mod

# A placeholder for validation only; the real one never touches these files.
_VALIDATE_PASSWORD = "validate-only"


@dataclass(frozen=True)
class Setting:
    key: str                  # dotted path in the YAML, e.g. "solar.array_w"
    group: str
    label: str
    help: str
    kind: str                 # "number" | "choice" | "bool" | "text" | "packs" (set from detection only)
    unit: str = ""
    choices: tuple = ()
    optional: bool = False    # empty is a meaningful value (e.g. "follow the Cerbo"), not just "unset"
    getter: object = None     # Config -> effective value


SETTINGS = (
    Setting("bank.source", "Batteries", "Battery source",
            "Where the bank's figures come from: one BMS per pack, summed (packs), or a "
            "single monitor such as a SmartShunt that measures the whole bank (monitor).",
            "choice", choices=model_mod.BANK_SOURCES, getter=lambda c: c.bank.source),
    Setting("packs", "Batteries", "Battery packs",
            "Each pack's BMS by its Cerbo ID, numbered. Set from what the Cerbo detects.",
            "packs", getter=lambda c: dict(c.packs)),
    Setting("bank.monitor_instance", "Batteries", "Battery monitor",
            "Only for a battery monitor, when the Cerbo lists more than one battery: the "
            "monitor's device instance. Empty uses the Cerbo's own system battery.",
            "number", optional=True, getter=lambda c: c.bank.monitor_instance),
    Setting("system.nominal_voltage", "Batteries", "System voltage",
            "The house battery bank's nominal voltage. Every battery alarm level scales from it.",
            "choice", " V", choices=profile_mod.NOMINAL_VOLTAGES,
            getter=lambda c: c.system.nominal_v),
    Setting("system.chemistry", "Batteries", "Battery type",
            "Only lithium iron phosphate is supported so far.",
            "choice", choices=profile_mod.CHEMISTRIES, getter=lambda c: c.system.chemistry),
    Setting("bank.capacity_ah", "Batteries", "Bank capacity",
            "Total amp-hours, used when the batteries don't report it themselves.",
            "number", " Ah", optional=True, getter=lambda c: c.bank.capacity_ah),
    Setting("solar.array_w", "Solar", "Solar panel watts",
            "Total of the watts on the panel labels.",
            "number", " W", getter=lambda c: c.solar.array_w),
    Setting("solar.array_voc_v", "Solar", "Array open-circuit voltage",
            "The panels' voltage in full sun with nothing drawing: one panel's for "
            "panels wired in parallel, times the count for panels in series. "
            "Measured, not the label (labels read 5-10% higher).",
            "number", " V", getter=lambda c: c.solar.array_voc_v),
    Setting("ble.enabled", "Bluetooth", "Read Bluetooth devices",
            "Lithionics packs and ACR, and temperature sensors, read over Bluetooth. "
            "Use Scan below to find them.", "bool", getter=lambda c: c.ble.enabled),
    Setting("ble.pack_addresses", "Bluetooth", "Battery packs (Bluetooth)",
            "Each Lithionics pack's Bluetooth address, by pack number. Set by Scan.",
            "map", getter=lambda c: {int(k): v for k, v in c.ble.pack_addresses.items()}),
    Setting("ble.acr_address", "Bluetooth", "ACR (Bluetooth)",
            "The Lithionics ACR's Bluetooth address. Set by Scan.",
            "text", optional=True, getter=lambda c: c.ble.acr_address or None),
    Setting("ble.cabin_sensors", "Bluetooth", "Temperature sensors",
            "Bluetooth thermometers by address, each with its name (cabin, fridge, "
            "outside...). Set by Scan.", "map", getter=lambda c: dict(c.ble.cabin_sensors)),
    Setting("drives.engine_charge_a", "Driving", "Engine charging current",
            "The most the engine can put into the house batteries: typical alternator "
            "peak if wired direct, or the DC-DC charger's rating.",
            "number", " A", getter=lambda c: c.drives.settings.engine_charge_a),
    Setting("drives.joint_temp_instance", "Driving", "Hot-joint temperature probe",
            "The Cerbo temperature probe on a joint in the charging path (its device "
            "instance). Drive reports watch how hot it gets. Empty if there is none.",
            "number", optional=True, getter=lambda c: c.drives.settings.joint_temp_instance),
    Setting("system.timezone", "Location", "Time zone",
            "An IANA zone such as America/Denver. Leave empty to follow the Cerbo's own "
            "setting.", "text", optional=True, getter=lambda c: c.timezone),
    Setting("ha_mqtt.enabled", "Home Assistant", "Publish to Home Assistant",
            "Optional: send readings, events and discovery to Home Assistant's MQTT "
            "broker, so they appear as HA entities. Cerberus's own screens don't need it.",
            "bool", getter=lambda c: c.ha_mqtt.enabled),
    Setting("ha_mqtt.host", "Home Assistant", "Home Assistant MQTT broker",
            "The broker's address (127.0.0.1 when HA runs on this box).",
            "text", getter=lambda c: c.ha_mqtt.host),
    Setting("ha_mqtt.port", "Home Assistant", "Broker port",
            "Usually 1883.", "number", getter=lambda c: c.ha_mqtt.port),
    Setting("weather.zip_code", "Location", "Home ZIP code",
            "Where the van lives, for the weather forecast when there is no GPS fix. "
            "Saving it looks up the home latitude and longitude below (once, at "
            "zippopotam.us). US only; elsewhere, enter latitude and longitude.",
            "text", optional=True, getter=lambda c: c.weather.zip_code),
    Setting("weather.latitude", "Location", "Home latitude",
            "Used for the weather forecast when there is no GPS fix.",
            "number", "°", getter=lambda c: c.weather.latitude),
    Setting("weather.longitude", "Location", "Home longitude",
            "Used for the weather forecast when there is no GPS fix.",
            "number", "°", getter=lambda c: c.weather.longitude),
)
BY_KEY = {s.key: s for s in SETTINGS}


def _get(tree: dict, key: str):
    node = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


# "Remove the override": the value falls back to config.yaml or the default.
# Distinct from None, which for an optional setting is a value of its own
# ("follow the Cerbo") and must be able to override config.yaml.
CLEAR = object()


def _set(tree: dict, key: str, value) -> None:
    parts = key.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    if value is CLEAR:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


def _read_yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _same(a, b, tolerance: float = 0.0) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= max(1e-6, tolerance * abs(float(b)))
    if isinstance(a, dict) and isinstance(b, dict):
        return {str(k): int(v) for k, v in a.items()} == {str(k): int(v) for k, v in b.items()}
    return a == b


def map_display(value: dict) -> str:
    if not value:
        return "None"
    def short(a):
        a = str(a)
        return a if len(a) <= 17 else a[:8] + "…"
    items = sorted(value.items(), key=lambda kv: str(kv[0]))
    return ", ".join(f"{k} = {short(v)}" if isinstance(k, int) else f"{v} ({short(k)})"
                     for k, v in items)


def _match(prop: dict, value) -> bool | None:
    """Whether a proposal agrees with the current value; None while learning.
    Learned numbers move a little day to day, so they match within
    learn.MATCH_TOLERANCE."""
    if prop.get("learning"):
        return None
    return _same(prop["value"], value,
                 learn_mod.MATCH_TOLERANCE if prop.get("learned") else 0.0)


def current(config_path: str | Path, detected: dict | None = None) -> list[dict]:
    """Every setting with its effective value and where that came from, and
    what setup detection proposes for it (cerberus.detect), if anything."""
    base = _read_yaml(Path(config_path))
    setup = _read_yaml(cfg_mod.setup_path(config_path))
    cfg = cfg_mod.from_raw(cfg_mod.read_raw(config_path), password=_VALIDATE_PASSWORD)
    out = []
    for s in SETTINGS:
        _, in_setup = _get(setup, s.key)
        _, in_config = _get(base, s.key)
        value = s.getter(cfg)
        prop = (detected or {}).get(s.key)
        out.append({
            "key": s.key, "group": s.group, "label": s.label, "help": s.help,
            "kind": s.kind, "unit": s.unit, "choices": list(s.choices),
            "optional": s.optional, "value": value,
            "display": (detect_mod.pack_display(value) if s.kind == "packs"
                        else map_display(value) if s.kind == "map" else None),
            "source": "setup" if in_setup else "config" if in_config else "default",
            "detected": None if prop is None else {**prop, "matches": _match(prop, value)},
        })
    return out


def _coerce(s: Setting, value):
    """The owner's input as the type the YAML should hold. "" removes the
    override, so it falls back to config.yaml or the default; None does the
    same, except for an optional setting, where it sets "none" explicitly."""
    if value is None:
        return None if s.optional else CLEAR
    if isinstance(value, str) and value.strip() == "":
        return CLEAR
    if s.kind == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise cfg_mod.ConfigError(f"{s.label}: {value!r} is not a number") from None
        return int(v) if v.is_integer() else v
    if s.kind == "bool":
        v = str(value).strip().lower()
        if v in ("true", "on", "yes", "1"):
            return True
        if v in ("false", "off", "no", "0"):
            return False
        raise cfg_mod.ConfigError(f"{s.label}: must be on or off")
    if s.kind == "map":
        if not isinstance(value, dict):
            raise cfg_mod.ConfigError(f"{s.label}: expected a map")
        return {(int(k) if str(k).isdigit() else str(k)): v for k, v in value.items()}
    if s.kind == "packs":
        if not isinstance(value, dict) or not value:
            raise cfg_mod.ConfigError(f"{s.label}: expected a map of Cerbo IDs to pack numbers")
        return {str(k): v for k, v in value.items()}
    if s.kind == "choice":
        for c in s.choices:
            if str(c) == str(value):
                return c
        raise cfg_mod.ConfigError(f"{s.label}: must be one of {', '.join(map(str, s.choices))}")
    return str(value).strip()


def apply(config_path: str | Path, changes: dict) -> list[dict]:
    """Validate `changes` ({key: value}; see _coerce for "" and None) against the
    whole merged configuration, then write setup.yaml. Raises ConfigError and
    writes nothing if the result would not load."""
    setup = build_setup(_read_yaml(Path(config_path)),
                        _read_yaml(cfg_mod.setup_path(config_path)), changes)
    write_setup(config_path, setup)
    return current(config_path)


def expand_zip(changes: dict, lookup=None) -> tuple[dict, str | None]:
    """A new home ZIP brings its latitude and longitude with it. Returns the
    changes to apply and a sentence saying what the ZIP resolved to."""
    lookup = lookup or geocode_mod.lookup_zip
    z = changes.get("weather.zip_code")
    if z is None or str(z).strip() == "":
        return changes, None
    try:
        hit = lookup(z)
    except geocode_mod.GeocodeError as err:
        raise cfg_mod.ConfigError(f"Home ZIP code: {err}") from None
    out = {**changes, "weather.zip_code": hit["zip"],
           "weather.latitude": hit["latitude"], "weather.longitude": hit["longitude"]}
    where = f" ({hit['place']})" if hit["place"] else ""
    return out, (f"ZIP {hit['zip']}{where} is at {hit['latitude']}, {hit['longitude']}; "
                 "the weather forecast uses that from now on.")


def build_setup(base: dict, setup: dict, changes: dict, password: str | None = None) -> dict:
    """`setup` with `changes` applied, validated merged over `base` exactly
    as the collector will load it. Raises ConfigError; writes nothing."""
    unknown = sorted(set(changes) - set(BY_KEY))
    if unknown:
        raise cfg_mod.ConfigError(f"not an editable setting: {', '.join(unknown)}")
    setup = dict(setup)
    for key, value in changes.items():
        _set(setup, key, _coerce(BY_KEY[key], value))
    cfg_mod.from_raw(cfg_mod.merge(base, setup), password=password or _VALIDATE_PASSWORD)
    return setup


def write_setup(config_path: str | Path, setup: dict) -> None:
    _write_atomic(cfg_mod.setup_path(config_path), setup)


def _write_atomic(path: Path, data: dict) -> None:
    header = ("# Written by the Cerberus UI (Settings). Values here override\n"
              "# config.yaml; delete a line to fall back to it.\n")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".setup-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
