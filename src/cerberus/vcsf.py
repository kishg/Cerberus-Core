"""VictronConnect settings files (.vcsf): decode, pick out the settings, diff.

A .vcsf is what VictronConnect's "Save settings" writes. Decoded 2026-11-10
from this van's MultiPlus exports: the 5-byte magic ``VCCFH``, the length of
the decompressed body as a big-endian uint32, then zlib-compressed JSON::

    {"appName": "VictronConnect", "appversion": "v6.33", "productid": "0x2705",
     "minfwversion": "0x568", "structureversion": 1, "description": "",
     "data": {"/Devices/0/Settings/AbsorptionVoltage": 14.4, ...}}

``data`` holds the device's D-Bus paths, the same ones the GX publishes over
MQTT (``vebus/<instance>/Devices/0/Settings/...``), so a file can be checked
against cerberus.device_settings. Unlike MQTT it includes the charger and
low-battery settings. It also holds live readings (AC, DC, uptime) from the
moment of the save; those are dropped here.

VE.Configure's .rvsc files are a different, undocumented binary format and
are not read.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass

MAGIC = b"VCCFH"
MAX_DECOMPRESSED = 2 * 1024 * 1024


class VcsfError(ValueError):
    pass


@dataclass(frozen=True)
class Vcsf:
    meta: dict          # everything but "data"
    data: dict          # path -> value, as saved

    @property
    def kind(self) -> str:
        """The GX service the paths belong to: vebus, solarcharger, or other."""
        if "/VebusChargeState" in self.data or self.data.get("/Mgmt/Connection") == "VE.Bus":
            return "vebus"
        if any(k.startswith(("/Pv/", "/Yield/")) for k in self.data):
            return "solarcharger"
        return "other"

    @property
    def serial(self) -> str | None:
        for k in ("/Devices/0/SerialNumber", "/Serial", "/SerialNumber"):
            v = self.data.get(k)
            if v:
                return str(v)
        return None

    @property
    def product_id(self) -> str | None:
        pid = self.meta.get("productid")
        if pid:
            return str(pid)
        v = self.data.get("/ProductId")
        return f"0x{v:04x}" if isinstance(v, int) else None

    @property
    def firmware(self) -> str | None:
        v = self.data.get("/FirmwareVersion")
        return f"{v:x}" if isinstance(v, int) else (str(v) if v else None)

    @property
    def device_key(self) -> str:
        """What ties successive files of one device together."""
        return self.serial or f"{self.kind}:{self.product_id or '?'}"

    def settings(self) -> dict:
        return {k: v for k, v in self.data.items() if is_setting(k)}


def decode(raw: bytes) -> Vcsf:
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 4:
        raise VcsfError("not a VictronConnect settings file (.vcsf)")
    (length,) = struct.unpack(">I", raw[len(MAGIC):len(MAGIC) + 4])
    if length > MAX_DECOMPRESSED:
        raise VcsfError("settings file is implausibly large")
    try:
        d = zlib.decompressobj()
        body = d.decompress(raw[len(MAGIC) + 4:], MAX_DECOMPRESSED + 1)
        doc = json.loads(body)
    except (zlib.error, ValueError):
        raise VcsfError("the settings file is damaged and can't be read") from None
    if len(body) != length or not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        raise VcsfError("the settings file is damaged and can't be read")
    return Vcsf(meta={k: v for k, v in doc.items() if k != "data"}, data=doc["data"])


# ---------------------------------------------------------------- settings

# Bookkeeping that sits under a Settings path but is not a setting.
_NOT_SETTINGS = re.compile(
    r"(/ReadProgress|/WriteProgress|/ResetRequired|/ResetToDefaults|/RequiresReset"
    r"|/RequiresAccessLevel|/Password|/SettingsChangedTime|/AccessLevelRequired)$")


def is_setting(path: str) -> bool:
    if path.startswith("/Update/"):
        return False
    if "/Settings/" in path:
        return not _NOT_SETTINGS.search(path)
    return bool(re.fullmatch(r"/(Devices/\d+/)?FirmwareVersion|/Ac/In/\d+/CurrentLimit"
                             r"|/Devices/\d+/Assistants", path))


# Names and units for the settings an owner is likely to care about. Others
# get a name from their path.
_KNOWN = {
    "AbsorptionVoltage": ("Absorption voltage", "V"),
    "FloatVoltage": ("Float voltage", "V"),
    "ChargeCurrent": ("Charge current", "A"),
    "MaximumAbsorptionTime": ("Maximum absorption time", "min"),
    "RepeatedAbsorptionTime": ("Repeated absorption time", "min"),
    "RepeatedAbsorptionInterval": ("Repeated absorption interval", "min"),
    "ChargeCharacteristic": ("Charge curve", ""),
    "LithiumBattery": ("Lithium battery", ""),
    "EnableReducedFloat": ("Storage mode (reduced float)", ""),
    "DisableCharger": ("Charger disabled", ""),
    "StopChargingBelowTemperature": ("Stop charging below", "°C"),
    "TemperatureCompensationSlope": ("Temperature compensation", "V/°C"),
    "InverterDcShutdownVoltage": ("DC input low shut-down", "V"),
    "InverterDcRestartVoltage": ("DC input low restart", "V"),
    "LowDcAlarmLevel": ("DC input low pre-alarm", "V"),
    "DoNotChargeVoltage": ("Don't charge below", "V"),
    "AcCurrentLimit": ("AC input current limit", "A"),
    "CurrentLimit": ("AC input current limit", "A"),
    "InverterOutputVoltage": ("Inverter output voltage", "V"),
    "PowerAssistEnabled": ("PowerAssist", ""),
    "AssistCurrentBoostFactor": ("PowerAssist boost factor", ""),
    "WeakAcInput": ("Weak AC input", ""),
    "AcceptWideInputFrequency": ("Accept wide input frequency", ""),
    "DisableAes": ("Search mode (AES) disabled", ""),
    "DisableGroundRelay": ("Ground relay disabled", ""),
    "DisableUBatVSense": ("Voltage sense disabled", ""),
    "FirmwareVersion": ("Firmware", ""),
}


def label(path: str) -> tuple[str, str]:
    """(name, unit) for a settings path."""
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if (m := re.fullmatch(r"/Devices/(\d+)/FirmwareVersion", path)):
        return f"Firmware (unit {m.group(1)})", ""
    if (m := re.fullmatch(r"/Ac/In/(\d+)/CurrentLimit", path)):
        return f"AC input {m.group(1)} current limit (now)", "A"
    if tail in _KNOWN:
        return _KNOWN[tail]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail)
    return words[:1].upper() + words[1:].lower(), ""


def fmt(path: str, value) -> str:
    """A value for people: volts to 2 dp, on/off for switches, minutes as minutes."""
    if value is None:
        return "not set"
    name, unit = label(path)
    tail = path.rsplit("/", 1)[-1]
    if tail == "FirmwareVersion" and isinstance(value, int):
        return f"v{value:x}"
    if isinstance(value, bool) or (unit == "" and value in (0, 1) and not isinstance(value, float)
                                   and _is_switch(tail)):
        return "on" if value else "off"
    if isinstance(value, float):
        value = round(value, 2)
        if value == int(value) and unit not in ("V",):
            value = int(value)
    if unit == "V" and isinstance(value, (int, float)):
        return f"{value:.2f} V"
    return f"{value} {unit}".strip()


def _is_switch(tail: str) -> bool:
    return tail.startswith(("Enable", "Disable", "Is", "Allow", "Accept")) or tail in (
        "LithiumBattery", "PowerAssistEnabled", "WeakAcInput", "UpsFunction",
        "DontStopAfter10hBulk", "SettingsLocked", "RemoteOverrulesAc1")


def same(a, b) -> bool:
    """Floats are saved as 14.399999618530273; compare to 1e-3."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-3
    return a == b


def diff(old: dict, new: dict) -> list[dict]:
    """Settings that differ between two files' settings(), in path order."""
    out = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
        if not same(a, b):
            name, unit = label(path)
            out.append({"path": path, "name": name, "unit": unit, "old": a, "new": b,
                        "old_text": fmt(path, a), "new_text": fmt(path, b)})
    return out
