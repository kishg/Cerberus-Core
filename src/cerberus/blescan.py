"""Finding a van's Bluetooth devices, so nobody types addresses into config.yaml.

The Settings screen's "Scan for Bluetooth devices" runs this from the UI:

1. **Listen** for advertisements a few seconds. A Govee H5179 is recognised
   from the advertisement alone (cerberus.govee). Lithionics devices are
   candidates by name ("Li3-<serial>" for packs; "ACR"/"Lithionics") or by
   offering the FFE0 UART service they all speak.
2. **Probe** each Lithionics candidate: connect a few seconds, subscribe to
   FFE1 and read what it streams, read-only. A pack sends "&,<n>,..." status
   frames, which carry its own pack number, and cell frames; the ACR sends
   "*,..." frames. So roles come from what a device says, not from guessing
   its name (the ACR's advertised name isn't on record yet).
3. **Propose** ble.enabled, ble.pack_addresses (numbered by the packs' own
   pack numbers when they report distinct ones, else by serial), ble.acr_address
   and ble.cabin_sensors, applied through the ordinary validated settings
   write. Temperature sensors are any brand cerberus.thermo decodes; the
   scan can't know where each one is, so the owner names them ("cabin",
   "fridge", "outside"; in a campground some will be the neighbours'), with
   the strongest signal suggested as "cabin".

A device the BLE sidecar is already connected to stops advertising, so a
scan on a running box only finds what isn't set up yet; the Settings screen
lists the configured devices beside the results. Addresses are what the OS
uses: per-host UUIDs on macOS, MAC addresses on Linux, which is why moving to
a new box means scanning again.

Read-only: listening and notify subscriptions only; nothing is written to a
device.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from . import ble as ble_mod
from .thermo import decode as decode_ambient

SCAN_S = 8.0
PROBE_S = 6.0
CONNECT_S = 15.0
UART_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
_LITHIONICS_NAME = re.compile(r"^li3-|lithionics|\bacr\b", re.I)
_SERIAL = re.compile(r"(\d+)\s*$")

PACK, ACR, CABIN, UNKNOWN = "pack", "acr", "sensor", "unknown"


@dataclass
class Found:
    address: str
    name: str
    rssi: int | None
    role: str = UNKNOWN
    pack_number: int | None = None       # the BMS's own pack number, from "&" frames
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"address": self.address, "name": self.name, "rssi": self.rssi,
                "role": self.role, "pack_number": self.pack_number, "detail": self.detail}


def lithionics_candidate(name: str, service_uuids) -> bool:
    return bool(_LITHIONICS_NAME.search(name or "")) or \
        UART_SERVICE in [str(u).lower() for u in (service_uuids or [])]


def classify_lines(lines: list[str]) -> tuple[str, int | None, str]:
    """(role, pack number, detail) from what a Lithionics device streamed."""
    pack_numbers = []
    cells = acr = None
    for line in lines:
        if ble_mod.parse_acr_frame(line):
            acr = ble_mod.parse_acr_frame(line)
        b = ble_mod.parse_frame_b(line)
        if b:
            pack_numbers.append(b["pack_id_reported"])
        a = ble_mod.parse_frame_a(line)
        if a:
            cells = a
    if acr and not (pack_numbers or cells):
        house = acr.get("house_v_cv")
        return ACR, None, f"house {house / 100:.2f} V" if house else ""
    if pack_numbers or cells:
        n = max(set(pack_numbers), key=pack_numbers.count) if pack_numbers else None
        detail = f"{cells['pack_v_cv'] / 100:.2f} V, {cells['soc_pct']}%" if cells else ""
        return PACK, n, detail
    return UNKNOWN, None, ""


async def _listen(timeout: float) -> list[Found]:
    from bleak import BleakScanner
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in seen.items():
        name = adv.local_name or dev.name or ""
        f = Found(address=addr, name=name, rssi=adv.rssi)
        reading = decode_ambient(adv.manufacturer_data, adv.service_data)
        if reading is not None:
            hum = "" if reading.humidity_pct is None else f", {reading.humidity_pct:.0f}%"
            f.role, f.detail = CABIN, f"{reading.model}: {reading.air_temp_f:.1f} °F{hum}"
        elif lithionics_candidate(name, adv.service_uuids):
            f.role = UNKNOWN
            f.detail = "candidate"
        else:
            continue
        out.append(f)
    return out


async def _probe(f: Found, seconds: float) -> None:
    from bleak import BleakClient
    buf = bytearray()

    def on_notify(_h, data: bytearray) -> None:
        buf.extend(data)
        while b"\r\n" in buf:
            line, _, rest = buf.partition(b"\r\n")
            buf[:] = rest
            f.lines.append(line.decode("ascii", errors="replace"))

    client = BleakClient(f.address)
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_S)
        try:
            await client.start_notify(ble_mod.FFE1_CHAR, on_notify)
            await asyncio.sleep(seconds)
            try:
                await client.stop_notify(ble_mod.FFE1_CHAR)
            except Exception:
                pass
        finally:
            await client.disconnect()
    except Exception as err:
        f.detail = f"couldn't read it ({type(err).__name__})"
        return
    f.role, f.pack_number, f.detail = classify_lines(f.lines)


async def scan_async(listen_s: float = SCAN_S, probe_s: float = PROBE_S) -> list[Found]:
    found = await _listen(listen_s)
    # One at a time: several simultaneous connects is where BLE stacks wobble.
    for f in found:
        if f.role == UNKNOWN:
            await _probe(f, probe_s)
    return [f for f in found if f.role != UNKNOWN]


def scan(listen_s: float = SCAN_S, probe_s: float = PROBE_S) -> list[dict]:
    """Blocking entry point for the UI's request thread."""
    return [f.as_dict() for f in asyncio.run(scan_async(listen_s, probe_s))]


def _serial(name: str) -> int:
    m = _SERIAL.search(name or "")
    return int(m.group(1)) if m else 0


def proposals(found: list[dict], cabin_name: str = "cabin") -> dict:
    """Settings changes for what a scan found, in the settings-catalog shape."""
    packs = [f for f in found if f["role"] == PACK]
    acrs = [f for f in found if f["role"] == ACR]
    cabins = sorted((f for f in found if f["role"] == CABIN), key=lambda f: -(f["rssi"] or -999))
    out: dict = {}
    if packs:
        numbers = [f["pack_number"] for f in packs]
        if all(n and n > 0 for n in numbers) and len(set(numbers)) == len(numbers):
            mapping, how = {n: f["address"] for n, f in zip(numbers, packs)}, "their own pack numbers"
        else:
            ordered = sorted(packs, key=lambda f: (_serial(f["name"]), f["name"]))
            mapping, how = {i: f["address"] for i, f in enumerate(ordered, start=1)}, "serial number"
        out["ble.pack_addresses"] = {"value": dict(sorted(mapping.items())),
                                     "reason": f"{len(packs)} Lithionics pack(s), numbered by {how}."}
    if len(acrs) == 1:
        out["ble.acr_address"] = {"value": acrs[0]["address"], "reason": "The Lithionics ACR."}
    if cabins:
        # Only the strongest is suggested, as the cabin: it is most likely
        # the van's own. Naming the rest is the owner's call (Settings).
        out["ble.cabin_sensors"] = {"value": {cabins[0]["address"]: cabin_name},
                                    "reason": f"{cabins[0]['detail'].split(':')[0]}, strongest signal."}
    if out:
        out["ble.enabled"] = {"value": True, "reason": "Bluetooth devices were found."}
    return out
