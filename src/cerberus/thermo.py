"""Bluetooth thermometers/hygrometers, decoded from their advertisements.

Owners buy whatever sensor they like for the cabin, the fridge or outside,
so Cerberus recognises the common ones rather than one model. All of these
put their readings in the advertisement itself: no pairing, no connection,
so listening costs none of the BLE connection slots the packs and ACR use.

| Format | Where | Status |
|---|---|---|
| Govee H5179 | manufacturer 0x8801 | verified on this van (cerberus.govee) |
| Govee H5072/H5075 | manufacturer 0xEC88, 3-byte packed value | from published decoders |
| Govee H5074 | manufacturer 0xEC88, int16 fields | from published decoders |
| Govee H5101/H5102/H5104/H5174/H5177 | manufacturer 0x0001, 3-byte packed | from published decoders |
| BTHome v2 (open standard; many sensors, and Xiaomi on pvvx firmware) | service data 0xFCD2 | from the spec (bthome.io) |
| Xiaomi LYWSD03MMC, pvvx custom firmware | service data 0x181A, 15 bytes | from the firmware's README |
| Xiaomi LYWSD03MMC, ATC1441 firmware | service data 0x181A, 13 bytes | from the firmware's README |
| SwitchBot Meter / Meter Plus | service data 0xFD3D or 0x0D00 | from published decoders |

"From published decoders/spec" means unit-tested against vectors built from
those descriptions, not yet against a physical sensor: the first owner with
one confirms it. Encrypted advertisements (BTHome or stock Xiaomi with a
bind key) are not decoded. Adding a brand is one function here.

Returns Fahrenheit (ambient, by the project convention) and drops readings
outside -40-80 C or 0-100 %, so a corrupted advertisement can't poison the
history.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import govee as govee_mod


def _uuid16(n: int) -> str:
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


BTHOME = _uuid16(0xFCD2)
ENV_SENSING = _uuid16(0x181A)            # pvvx / ATC1441
SWITCHBOT = (_uuid16(0xFD3D), _uuid16(0x0D00))
GOVEE_EC88 = 0xEC88
GOVEE_0001 = 0x0001


@dataclass(frozen=True)
class AmbientReading:
    air_temp_f: float
    humidity_pct: float | None
    battery_pct: int | None
    model: str


def _reading(temp_c: float, humidity: float | None, battery, model: str) -> AmbientReading | None:
    if not -40.0 <= temp_c <= 80.0:
        return None
    if humidity is not None and not 0.0 <= humidity <= 100.0:
        return None
    if battery is not None and not 0 <= battery <= 100:
        battery = None
    return AmbientReading(round(temp_c * 9 / 5 + 32, 1),
                          None if humidity is None else round(humidity, 1),
                          None if battery is None else int(battery), model)


def _govee_packed(b: bytes) -> tuple[float, float]:
    """Govee's 3-byte big-endian value: temp*10000 + humidity*10, sign in bit 23."""
    raw = int.from_bytes(b[:3], "big")
    neg = bool(raw & 0x800000)
    raw &= 0x7FFFFF
    temp = (raw // 1000) / 10.0
    return (-temp if neg else temp), (raw % 1000) / 10.0


def govee(mfr: dict) -> AmbientReading | None:
    h5179 = govee_mod.decode(mfr)
    if h5179 is not None:
        return AmbientReading(h5179.air_temp_f, h5179.humidity_pct, h5179.battery_pct, "Govee H5179")
    d = mfr.get(GOVEE_EC88)
    if d is not None:
        if len(d) == 6 and d[0] == 0x00:                              # H5072 / H5075
            t, h = _govee_packed(d[1:4])
            return _reading(t, h, d[4], "Govee H5075")
        if len(d) == 7 and d[0] == 0x00:                              # H5074
            t = struct.unpack_from("<h", d, 1)[0] / 100.0
            h = struct.unpack_from("<H", d, 3)[0] / 100.0
            return _reading(t, h, d[5], "Govee H5074")
    d = mfr.get(GOVEE_0001)
    if d is not None and len(d) >= 6 and d[0] == 0x01 and d[1] == 0x01:   # H5101/2/4, H5174/7
        t, h = _govee_packed(d[2:5])
        return _reading(t, h, d[5] & 0x7F, "Govee H51xx")
    return None


# BTHome v2 object ids we read, and every id's payload size so unknown
# objects can be skipped (only a prefix of the table is needed: objects come
# in id order, and parsing stops at an id it can't size).
_BTHOME_SIZES = {0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x04: 3, 0x05: 3, 0x06: 2, 0x07: 2,
                 0x08: 2, 0x09: 1, 0x0A: 3, 0x0B: 3, 0x0C: 2, 0x0D: 2, 0x0E: 2, 0x0F: 1,
                 0x10: 1, 0x11: 1, 0x12: 2, 0x13: 2, 0x14: 2, 0x15: 1, 0x16: 1, 0x17: 1,
                 0x18: 1, 0x19: 1, 0x1A: 1, 0x1B: 1, 0x1C: 1, 0x1D: 1, 0x1E: 1, 0x1F: 1,
                 0x20: 1, 0x21: 1, 0x22: 1, 0x23: 1, 0x24: 1, 0x25: 1, 0x26: 1, 0x27: 1,
                 0x28: 1, 0x29: 1, 0x2A: 1, 0x2B: 1, 0x2C: 1, 0x2D: 1, 0x2E: 1, 0x2F: 1,
                 0x45: 2, 0x46: 1}


def bthome(service: dict) -> AmbientReading | None:
    d = service.get(BTHOME)
    if not d:
        return None
    info = d[0]
    if info & 0x01 or (info >> 5) != 2:          # encrypted, or not v2
        return None
    temp = hum = batt = None
    i = 1
    while i < len(d):
        oid = d[i]
        size = _BTHOME_SIZES.get(oid)
        if size is None or i + 1 + size > len(d):
            break
        v = d[i + 1:i + 1 + size]
        if oid == 0x01:
            batt = v[0]
        elif oid == 0x02:
            temp = struct.unpack("<h", v)[0] / 100.0
        elif oid == 0x45:
            temp = struct.unpack("<h", v)[0] / 10.0
        elif oid == 0x03:
            hum = struct.unpack("<H", v)[0] / 100.0
        elif oid == 0x2E:
            hum = float(v[0])
        i += 1 + size
    return None if temp is None else _reading(temp, hum, batt, "BTHome")


def xiaomi_custom(service: dict) -> AmbientReading | None:
    d = service.get(ENV_SENSING)
    if d is None:
        return None
    if len(d) == 15:                              # pvvx: little-endian
        t = struct.unpack_from("<h", d, 6)[0] / 100.0
        h = struct.unpack_from("<H", d, 8)[0] / 100.0
        return _reading(t, h, d[12], "Xiaomi (pvvx)")
    if len(d) == 13:                              # ATC1441: big-endian
        t = struct.unpack_from(">h", d, 6)[0] / 10.0
        return _reading(t, float(d[8]), d[9], "Xiaomi (ATC)")
    return None


def switchbot(service: dict) -> AmbientReading | None:
    for uuid in SWITCHBOT:
        d = service.get(uuid)
        if d is None or len(d) < 6:
            continue
        if (d[0] & 0x7F) not in (0x54, 0x69):     # 'T' Meter, 'i' Meter Plus
            return None
        t = (d[4] & 0x7F) + (d[3] & 0x0F) / 10.0
        if not d[4] & 0x80:
            t = -t
        return _reading(t, float(d[5] & 0x7F), d[2] & 0x7F, "SwitchBot Meter")
    return None


def decode(manufacturer_data: dict | None, service_data: dict | None = None) -> AmbientReading | None:
    """Any supported sensor's reading, or None. Never raises: this runs in a
    scanner callback shared with the pack connections."""
    mfr = dict(manufacturer_data or {})
    svc = {str(k).lower(): bytes(v) for k, v in (service_data or {}).items()}
    for fn in (lambda: govee(mfr), lambda: bthome(svc), lambda: xiaomi_custom(svc),
               lambda: switchbot(svc)):
        try:
            r = fn()
        except (struct.error, IndexError, ValueError):
            r = None
        if r is not None:
            return r
    return None
