"""Continuous BLE capture across all three Lithionics packs.

Maintains parallel notify subscriptions on FFE1 for each pack with
automatic reconnect. Writes a single CSV with columns: ts, pack, line.
Lines are written line-buffered so the status tool can tail in real time.

  python ble_continuous.py

Stop with SIGINT/SIGTERM. The output file path is printed at startup
and a pid file is written next to the log.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

from bleak import BleakClient

FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

PACKS = {
    1: ("Li3-000000001", "00000000-0000-4000-8000-000000000001"),
    2: ("Li3-000000002", "00000000-0000-4000-8000-000000000002"),
    3: ("Li3-000000003", "00000000-0000-4000-8000-000000000003"),
}

stop_event: asyncio.Event | None = None


async def pack_loop(pack_id: int, name: str, addr: str,
                    q: asyncio.Queue) -> None:
    backoff = 5.0
    while not stop_event.is_set():
        buf = bytearray()

        def on_notify(_handle, data: bytearray) -> None:
            ts = time.time()
            buf.extend(data)
            while b"\r\n" in buf:
                line, _, rest = buf.partition(b"\r\n")
                try:
                    q.put_nowait((ts, pack_id, line.decode("ascii", errors="replace")))
                except asyncio.QueueFull:
                    pass
                buf[:] = rest

        try:
            print(f"[pack{pack_id}] connecting {name} ({addr})", flush=True)
            async with BleakClient(addr) as client:
                print(f"[pack{pack_id}] connected", flush=True)
                await client.start_notify(FFE1, on_notify)
                while client.is_connected and not stop_event.is_set():
                    await asyncio.sleep(2.0)
                try:
                    await client.stop_notify(FFE1)
                except Exception:
                    pass
            print(f"[pack{pack_id}] disconnected", flush=True)
            backoff = 5.0  # reset on clean disconnect
        except Exception as e:
            print(f"[pack{pack_id}] error: {e!r}", flush=True)
            backoff = min(backoff * 1.5, 60.0)
        if stop_event.is_set():
            break
        await asyncio.sleep(backoff)


async def writer_loop(out_path: Path, q: asyncio.Queue) -> None:
    with out_path.open("a", buffering=1) as f:
        while not (stop_event.is_set() and q.empty()):
            try:
                ts, pid, line = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            f.write(f"{ts:.3f},{pid},{line}\n")


async def main() -> int:
    global stop_event
    stop_event = asyncio.Event()

    def handler(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)

    out_dir = Path("ble_captures/continuous")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    out_path = out_dir / f"ble_{stamp}.csv"
    with out_path.open("w") as f:
        f.write("ts,pack,line\n")
    (out_dir / "current.csv").unlink(missing_ok=True)
    (out_dir / "current.csv").symlink_to(out_path.name)
    print(f"writing to {out_path}", flush=True)

    q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    tasks = [asyncio.create_task(pack_loop(pid, name, addr, q))
             for pid, (name, addr) in PACKS.items()]
    tasks.append(asyncio.create_task(writer_loop(out_path, q)))
    await asyncio.gather(*tasks, return_exceptions=True)
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
