"""Govee H5179 cabin sensor — decoded from BLE advertisements, no connection.

Why this exists
---------------
The van had no ambient temperature anywhere. The only readings were the three
Lithionics pack probes, and they are useless as a proxy: daily DC load
correlated with pack temperature at **r = +0.016** across 34 days. The packs sit
in a cabinet with thermal mass, self-heat under charge, and have never been
recorded below 20.7 C.

That gap blocked a real question. The van's seasonal energy balance has two
terms moving in opposite directions — solar falls with day length, but load
falls too, because the fridge's compressor duty scales with the delta-T it
pumps against and the MaxxFan runs less when it is cool. Outdoor air from
met.no covers the weather half. This covers the half that actually drives both:
the temperature *inside the van*.

The sensor was already in the van, found in a BLE scan on 2026-09-24 at
-53 dBm. No hardware was needed.

Passive, deliberately
---------------------
The H5179 puts its readings in the advertisement itself, so this never
connects. That matters on this rig: the sidecar already holds three
simultaneous connections to the packs, and CoreBluetooth has a finite number of
slots. A listener costs none of them and cannot disturb the pack links, which
had just come back after three weeks out of range.

Wire format
-----------
Manufacturer data under company id **0x8801**, 9 bytes, captured live::

    ec 00 01 01 34 08 cc 10 64   ->  21.00 C, 43.0 % RH, battery 100 %
    ec 00 01 01 3e 08 cc 10 64   ->  21.10 C, 43.0 % RH, battery 100 %

    [0:4]  ec 00 01 01   fixed preamble
    [4:6]  int16  LE     temperature, hundredths of a degree C
    [6:8]  uint16 LE     relative humidity, hundredths of a percent
    [8]    uint8         battery percent

Verified against two independent references at capture time: outdoor air read
64.2 F and the packs 22.3 C, putting a 69.8 F cabin sensibly between them.

Units
-----
Returns **Fahrenheit**. The project convention is ambient in F and equipment in
C, and this is ambient — the same reason ``weather_samples.air_temp_f`` carries
its unit in the column name. Temperature is signed: this sensor will read below
zero in winter, which is when the cold-soak charge lockout question matters.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

GOVEE_COMPANY_ID = 0x8801
_PREAMBLE = bytes.fromhex("ec000101")
_EXPECTED_LEN = 9


@dataclass(frozen=True)
class GoveeReading:
    air_temp_f: float
    humidity_pct: float
    battery_pct: int


def decode(manufacturer_data: dict[int, bytes]) -> GoveeReading | None:
    """Decode one advertisement, or None if it is not an H5179 payload.

    Returns None rather than raising for anything unexpected: this runs inside
    a scanner callback shared with the pack connections, and a malformed
    advertisement from any passing device must not be able to disturb them.
    """
    data = (manufacturer_data or {}).get(GOVEE_COMPANY_ID)
    if data is None or len(data) != _EXPECTED_LEN:
        return None
    if not data.startswith(_PREAMBLE):
        return None
    try:
        temp_c = struct.unpack_from("<h", data, 4)[0] / 100.0
        humidity = struct.unpack_from("<H", data, 6)[0] / 100.0
        battery = data[8]
    except struct.error:
        return None
    # Sanity bounds. A corrupted advertisement should be dropped, not stored:
    # a bogus -300 C would silently poison any correlation built on this table.
    if not (-40.0 <= temp_c <= 80.0) or not (0.0 <= humidity <= 100.0):
        return None
    return GoveeReading(
        air_temp_f=round(temp_c * 9.0 / 5.0 + 32.0, 1),
        humidity_pct=round(humidity, 1),
        battery_pct=int(battery),
    )
