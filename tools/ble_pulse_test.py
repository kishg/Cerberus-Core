"""Simultaneous load-pulse capture across all three Lithionics packs.

Connects to all three BLE serial-over-BLE streams in parallel, prints
countdown prompts telling you when to switch the load on and off, and
dumps a CSV plus a per-field baseline-vs-load comparison.

  python ble_pulse_test.py
"""
from __future__ import annotations

import asyncio
import statistics
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

DURATION = 90  # total seconds
LOAD_ON = 15   # turn on at t=15
LOAD_OFF = 45  # turn off at t=45


async def capture_pack(pack_id: int, addr: str, stop_event: asyncio.Event,
                       lines_out: list[tuple[float, int, str]], t0: float) -> None:
    buf = bytearray()

    def on_notify(_handle, data: bytearray) -> None:
        ts = time.time() - t0
        buf.extend(data)
        while b"\r\n" in buf:
            line, _, rest = buf.partition(b"\r\n")
            txt = line.decode("ascii", errors="replace")
            lines_out.append((ts, pack_id, txt))
            buf[:] = rest

    try:
        async with BleakClient(addr) as client:
            await client.start_notify(FFE1, on_notify)
            try:
                await stop_event.wait()
            finally:
                try:
                    await client.stop_notify(FFE1)
                except Exception:
                    pass
    except Exception as e:
        print(f"!!! pack {pack_id} ({addr}) failed: {e}", flush=True)


async def announcer(stop_event: asyncio.Event, total: int, t0: float) -> None:
    schedule = [
        (0,        "=== capture started — keep load OFF (baseline) ==="),
        (LOAD_ON,  "*** TURN LOAD ON NOW ***"),
        (LOAD_OFF, "*** TURN LOAD OFF NOW ***"),
        (75,       "=== recovery phase — capture ends in 15s ==="),
    ]
    i = 0
    while True:
        elapsed = time.time() - t0
        while i < len(schedule) and elapsed >= schedule[i][0]:
            print(f"[t={int(elapsed):3d}s] {schedule[i][1]}", flush=True)
            i += 1
        if elapsed >= total:
            break
        await asyncio.sleep(0.25)
    stop_event.set()


def classify(line: str) -> str:
    s = line.strip()
    if s.startswith("&"):
        return "B"
    if "," in s:
        return "A"
    return "?"


def parse_fields(line: str) -> list:
    parts = [p.strip() for p in line.strip().split(",")]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return out


def report(lines: list[tuple[float, int, str]]) -> None:
    print()
    print("=" * 70)
    print("Captured: total frames per pack")
    for pid in (1, 2, 3):
        n = sum(1 for _, p, _ in lines if p == pid)
        print(f"  pack {pid}: {n} frames")

    def window(pid, frame_type, t_lo, t_hi):
        return [parse_fields(line) for ts, p, line in lines
                if p == pid and classify(line) == frame_type and t_lo <= ts < t_hi]

    # Compute mean per field for baseline window (5..LOAD_ON-2) vs load window (LOAD_ON+5..LOAD_OFF-2)
    BASE_LO, BASE_HI = 5.0, float(LOAD_ON - 2)
    LOAD_LO, LOAD_HI = float(LOAD_ON + 5), float(LOAD_OFF - 2)

    for frame_type, label in (("A", "Frame A (cells/SoC)"), ("B", "Frame B (status/&)")):
        print()
        print(f"--- {label} — baseline ({BASE_LO:.0f}-{BASE_HI:.0f}s) vs load ({LOAD_LO:.0f}-{LOAD_HI:.0f}s) ---")
        for pid in (1, 2, 3):
            base = window(pid, frame_type, BASE_LO, BASE_HI)
            load = window(pid, frame_type, LOAD_LO, LOAD_HI)
            if not base or not load:
                print(f"  pack {pid}: not enough samples (base={len(base)}, load={len(load)})")
                continue
            n_fields = min(len(base[0]), len(load[0]))
            base_means = []
            load_means = []
            for i in range(n_fields):
                bvals = [r[i] for r in base if isinstance(r[i], int)]
                lvals = [r[i] for r in load if isinstance(r[i], int)]
                base_means.append(statistics.mean(bvals) if bvals else None)
                load_means.append(statistics.mean(lvals) if lvals else None)
            print(f"  pack {pid} (base n={len(base)}, load n={len(load)}):")
            for i in range(n_fields):
                bm, lm = base_means[i], load_means[i]
                if bm is None or lm is None:
                    continue
                delta = lm - bm
                flag = " <<<" if abs(delta) >= 1 else ""
                print(f"    field[{i:2d}]: base={bm:10.2f}  load={lm:10.2f}  Δ={delta:+8.2f}{flag}")


async def main() -> int:
    stop = asyncio.Event()
    lines: list[tuple[float, int, str]] = []
    t0 = time.time()
    tasks = [asyncio.create_task(capture_pack(pid, addr, stop, lines, t0))
             for pid, (_, addr) in PACKS.items()]
    tasks.append(asyncio.create_task(announcer(stop, DURATION, t0)))
    print("connecting to all three packs (may take a few seconds)...")
    await asyncio.gather(*tasks, return_exceptions=True)

    out = Path("ble_captures")
    out.mkdir(parents=True, exist_ok=True)
    csv_p = out / f"pulse_{int(time.time())}.csv"
    with csv_p.open("w") as f:
        f.write("t_sec,pack,line\n")
        for ts, pid, txt in lines:
            f.write(f"{ts:.3f},{pid},{txt}\n")
    print(f"\nwrote {len(lines)} lines to {csv_p}")

    report(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
