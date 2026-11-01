"""Lithionics NeverDie BMS BLE capture tool.

Two modes:
  python ble_capture.py scan [timeout_seconds]
  python ble_capture.py capture <address> <seconds> [out_dir]

The BMS exposes its serial console as a transparent UART over BLE:
service FFE0, notify characteristic FFE1, plain ASCII lines ending in CRLF.
This script reassembles BLE notify chunks back into whole lines and writes
both the raw notify packets and the decoded CSV lines to disk for analysis.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

FFE0_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
FFE1_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"


def _has_ffe0(service_uuids) -> bool:
    return any(str(s).lower().startswith("0000ffe0") or str(s).lower() == "ffe0"
               for s in (service_uuids or []))


async def scan(timeout: float = 10.0) -> None:
    print(f"scanning for {timeout}s...")
    devs = await BleakScanner.discover(timeout=timeout, return_adv=True)
    candidates = []
    for addr, (dev, adv) in devs.items():
        name = dev.name or adv.local_name or ""
        services = list(adv.service_uuids or [])
        is_cand = _has_ffe0(services) or "neverdie" in name.lower() or "lithion" in name.lower()
        marker = "  <-- candidate" if is_cand else ""
        rssi = getattr(adv, "rssi", None)
        print(f"  {addr}  rssi={rssi}  name={name!r}  services={services}{marker}")
        if is_cand:
            candidates.append((addr, name))
    print()
    print(f"{len(candidates)} candidate(s):")
    for addr, name in candidates:
        print(f"  {addr}  {name!r}")


async def capture(address: str, seconds: float, out_dir: str) -> None:
    buf = bytearray()
    lines: list[tuple[float, str]] = []
    raw_records: list[tuple[float, bytes]] = []

    def on_notify(_handle, data: bytearray) -> None:
        ts = time.time()
        raw_records.append((ts, bytes(data)))
        buf.extend(data)
        while b"\r\n" in buf:
            line, _, rest = buf.partition(b"\r\n")
            txt = line.decode("ascii", errors="replace")
            lines.append((ts, txt))
            buf[:] = rest

    print(f"connecting to {address}...")
    async with BleakClient(address) as client:
        print(f"connected. subscribing FFE1 for {seconds}s...")
        await client.start_notify(FFE1_CHAR, on_notify)
        try:
            await asyncio.sleep(seconds)
        finally:
            await client.stop_notify(FFE1_CHAR)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    safe_addr = address.replace(":", "_").replace("-", "_")
    raw_p = out / f"raw_{safe_addr}_{stamp}.log"
    lines_p = out / f"lines_{safe_addr}_{stamp}.csv"
    with raw_p.open("w") as f:
        for ts, b in raw_records:
            f.write(f"{ts:.6f} {b.hex()}\n")
    with lines_p.open("w") as f:
        f.write("ts,line\n")
        for ts, txt in lines:
            f.write(f"{ts:.6f},{txt}\n")

    print(f"captured {len(raw_records)} notifications, {len(lines)} complete lines")
    print(f"  raw:   {raw_p}")
    print(f"  lines: {lines_p}")
    print()
    print("--- last 10 lines ---")
    for ts, txt in lines[-10:]:
        print(f"  {txt}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "scan":
        t = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
        asyncio.run(scan(t))
    elif cmd == "capture":
        if len(sys.argv) < 4:
            print("usage: capture <address> <seconds> [out_dir]")
            return 1
        addr = sys.argv[2]
        secs = float(sys.argv[3])
        out = sys.argv[4] if len(sys.argv) > 4 else "ble_captures"
        asyncio.run(capture(addr, secs, out))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
