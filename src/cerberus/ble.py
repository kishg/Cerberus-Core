"""BLE collector for Lithionics NeverDie BMS data.

Runs as a sidecar to the main MQTT loop. Maintains parallel notify
subscriptions on each pack's FFE0/FFE1 (transparent UART over BLE),
reassembles the ASCII CRLF stream, parses the two frame types, and
writes throttled samples to `cell_samples` and `pack_ble_samples`.

If `ble.acr_address` is set it also holds the Lithionics ACR, which speaks the
same protocol, and writes its stream frame to `acr_samples`. Its settings are
read back with `$LIST` (read-only) into `acr_settings`.

Run with:
    python -m cerberus.ble --config config.yaml --log-level INFO
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

import yaml
from bleak import BleakClient, BleakScanner

from . import config as cfg_mod
from . import obd as obd_mod
from .db import Db
from .events import Event, Severity
from .thermo import decode as decode_ambient
from .log import setup_logging

log = logging.getLogger("cerberus.ble")

FFE1_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"



class _ConnectFailureLog:
    """Collapses a repeating connect failure into one line plus a periodic summary.

    Out of BLE range — the laptop in the house, the packs in the van — every
    pack fails to connect every ~90 s, forever, and says exactly the same thing
    each time. Measured 2026-10-01 after 2.6 days parked: **5,773 lines/day**
    across three packs, 5.2 MB of log, on track for ~250 MB/year. The plist
    comment estimated ~4,300/day, so it is worse than assumed.

    None of that volume carries information. The condition is correct and
    expected (see the RANGE note in launchd/com.cerberus.ble.plist), it is
    fully described by its first line, and it ends by itself on return to the
    van. Rotation would only manage the symptom.

    So: first failure of a sequence at WARNING with the exception, the rest at
    DEBUG, one WARNING summary per hour while it persists so the log still
    shows the condition is ongoing, and a line on recovery saying how many
    attempts it took. Message prefixes are unchanged so existing greps for
    "pack N: connecting" and "pack N: error" still work.
    """

    SUMMARY_INTERVAL_SECONDS = 3600.0

    def __init__(self, label: str) -> None:
        self._label = label
        self._count = 0
        self._first_error = ""
        self._since = 0.0
        self._last_summary = 0.0

    def connecting(self, addr: str) -> None:
        # The address is worth printing when a sequence starts and worthless on
        # the four-hundredth retry of the same one.
        if self._count == 0:
            log.info("%s: connecting %s", self._label, addr)
        else:
            log.debug("%s: connecting %s (attempt %d)",
                      self._label, addr, self._count + 1)

    def failure(self, exc: BaseException, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._count += 1
        if self._count == 1:
            self._first_error = repr(exc)
            self._since = now
            self._last_summary = now
            log.warning("%s: error: %r", self._label, exc)
            return
        log.debug("%s: error: %r (consecutive %d)", self._label, exc, self._count)
        if now - self._last_summary >= self.SUMMARY_INTERVAL_SECONDS:
            self._last_summary = now
            log.warning(
                "%s: still unreachable — %d consecutive failures over %.1f h, "
                "first was %s",
                self._label, self._count, (now - self._since) / 3600.0,
                self._first_error,
            )

    def success(self) -> None:
        if self._count:
            log.info("%s: connected after %d failed attempt(s)",
                     self._label, self._count)
        else:
            log.info("%s: connected", self._label)
        self._count = 0
        self._first_error = ""


class _ElmChannel:
    """Request/response framing for an ELM327 over a BLE serial characteristic.

    A reply is terminated by the adapter's ``>`` prompt rather than by length,
    so a read has to accumulate notifications until the prompt shows up. Calls
    are serialised behind a lock because the protocol carries no request ids:
    two commands in flight at once would let one's answer be attributed to the
    other, and a plausible-but-wrong RPM is worse here than no RPM.
    """

    def __init__(self, client: BleakClient, write_char: str, notify_char: str) -> None:
        self._client = client
        self._write_char = write_char
        self._notify_char = notify_char
        self._buf = ""
        self._prompt = asyncio.Event()
        self._lock = asyncio.Lock()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._buf += data.decode("ascii", errors="replace")
        if ">" in self._buf:
            self._prompt.set()

    async def start(self) -> None:
        await self._client.start_notify(self._notify_char, self._on_notify)

    async def stop(self) -> None:
        try:
            await self._client.stop_notify(self._notify_char)
        except Exception:
            pass

    async def ask(self, cmd: str, timeout: float) -> str:
        """Send one command, return whatever arrived before the prompt.

        On timeout this returns the partial buffer instead of raising. A
        truncated reply decodes to None, which is indistinguishable from "no
        answer" downstream — and far better than letting one slow PID tear
        down a connection that the other four are using happily.

        No chunking: every command here is at most six characters plus CR,
        comfortably inside the smallest BLE MTU.
        """
        async with self._lock:
            self._buf = ""
            self._prompt.clear()
            await self._client.write_gatt_char(
                self._write_char, (cmd + "\r").encode("ascii")
            )
            try:
                await asyncio.wait_for(self._prompt.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                log.debug("obd: %s timed out after %.1fs", cmd, timeout)
            return self._buf


def parse_frame_a(line: str) -> dict | None:
    """Cell/SoC frame, e.g. "1336,334,334,334,334,80,73,0,99,000000"."""
    s = line.strip()
    if not s or s.startswith("&"):
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 10:
        return None
    try:
        return {
            "pack_v_cv": int(parts[0]),
            "cells_cv": [int(parts[i]) for i in (1, 2, 3, 4)],
            "field5": int(parts[5]),
            "field6": int(parts[6]),
            "current_a": int(parts[7]),
            "soc_pct": int(parts[8]),
            "flags_hex": parts[9],
        }
    except ValueError:
        return None


def parse_frame_b(line: str) -> dict | None:
    """Status frame, e.g. "&,3,306,589413,0144,2300,FF03,0000"."""
    s = line.strip()
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 8 or parts[0] != "&":
        return None
    try:
        return {
            "pack_id_reported": int(parts[1]),
            "temp_dc": int(parts[2]),
            "big_counter": int(parts[3]),
            "field4": int(parts[4]),
            "nominal_field": int(parts[5]),
            "config_hex": parts[6],
            "trailer_hex": parts[7],
        }
    except ValueError:
        return None


ACR_INT_FIELDS = ("field1", "field2", "chassis_v_cv", "house_v_cv",
                  "field5", "field6", "field7", "chassis_present", "version")
# Everything but the two voltages, which move every frame. A change in any of
# these is written at once rather than waiting out acr_sample_seconds.
ACR_CHANGE_FIELDS = ("field1", "field2", "field5", "field6", "field7",
                     "chassis_present", "version")

# ACR modes (field 1) seen only with the engine running: 1 crank (chassis
# only), 2 alternator charging the house, 4 the high-voltage cut-out while the
# alternator runs. Decoded from a 1 s capture of a crank and idle on
# 2026-11-01. 0 is apart and 3 the house supporting the chassis, both seen
# with the engine off (after a stop, and when the driver's door wakes it).
ACR_ENGINE_MODES = frozenset({1, 2, 4})
# An ACR frame newer than this is a live reading of the engine's state.
ACR_FRESH_S = 120


def parse_acr_frame(line: str) -> dict | None:
    """ACR stream frame, e.g. "*,0,0,1261,1325,0,0,0,1,6002".

    Lenient on shape on purpose: only one parked capture has been decoded, so
    a short frame or a non-numeric field becomes None rather than a dropped
    line. The raw line always survives.
    """
    s = line.strip()
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 2 or parts[0] != "*":
        return None
    out: dict = {"raw": s, **dict.fromkeys(ACR_INT_FIELDS)}
    for name, val in zip(ACR_INT_FIELDS, parts[1:]):
        try:
            out[name] = int(val)
        except ValueError:
            out[name] = None
    return out


ACR_LIST_CMD = b"$LIST\r\n"
# Measured by hand 2026-10-29: the whole reply (8 lines on 6.0.05) arrives
# well inside this. Stream frames keep coming during the wait.
ACR_LIST_SETTLE_S = 3.0
# Let the connection settle before the first command.
ACR_LIST_FIRST_DELAY_S = 5.0


def parse_acr_list(lines: list[str]) -> tuple[str | None, dict]:
    """The `$LIST` reply: a header line and "Name=value" lines, e.g.

        Lithionics ACR v6.0.05
        Version=6005
        House Full Voltage=1420

    Returns (header, {name: int or str}). Anything else ("OK", "ERROR",
    a stray frame) is ignored.
    """
    firmware, settings = None, {}
    for line in lines:
        s = line.strip()
        if s.startswith("Lithionics ACR"):
            firmware = s
            continue
        name, sep, value = s.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name or name.startswith(("*", "$")):
            continue
        try:
            settings[name] = int(value)
        except ValueError:
            settings[name] = value
    return firmware, settings


class BleCollector:
    """Connects to N BLE peripherals, reassembles their CRLF stream, and
    coalesces Frame A + Frame B into one row per pack per
    sample_downsample_seconds."""

    def __init__(self, cfg: cfg_mod.BleConfig, db: Db,
                 obd_cfg: cfg_mod.ObdConfig | None = None) -> None:
        self.cfg = cfg
        self.db = db
        # Optional so existing callers and tests constructing a two-arg
        # BleCollector keep working; None means "no adapter configured".
        self.obd_cfg = obd_cfg
        self._stop = asyncio.Event()
        self._last_a: dict[int, dict] = {}
        self._last_b: dict[int, dict] = {}
        self._last_write_ts: dict[int, int] = {}
        self._mismatch_warned: set[int] = set()
        self._acr_last: dict | None = None
        self._acr_last_write_ts = 0
        self._acr_unknown_warned = False
        # Reply lines collected while a `$LIST` is outstanding, else None.
        self._acr_list: list[str] | None = None
        # The ACR's latest mode and when it was heard, kept for every frame
        # (not just the stored ones): the OBD loop wakes on a change into
        # ACR_ENGINE_MODES instead of trusting chassis voltage.
        self._acr_mode: int | None = None
        self._acr_seen_ts = 0.0
        self._engine_hint = asyncio.Event()

    def shutdown(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        obd_on = self.obd_cfg is not None and self.obd_cfg.enabled
        # A temperature sensor alone is enough (a van without Lithionics).
        if not (self.cfg.pack_addresses or self.cfg.acr_address
                or self.cfg.cabin_sensors or obd_on):
            log.warning("no Bluetooth devices configured; nothing to collect")
            return
        tasks = [
            asyncio.create_task(self._pack_loop(pid, addr))
            for pid, addr in self.cfg.pack_addresses.items()
        ]
        if self.cfg.acr_address:
            tasks.append(asyncio.create_task(self._uart_loop(
                "acr", self.cfg.acr_address, self._dispatch_acr,
                self._acr_settings_loop)))
        if self.cfg.cabin_sensors:
            tasks.append(asyncio.create_task(self._cabin_loop()))
        if obd_on:
            tasks.append(asyncio.create_task(self._obd_loop()))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cabin_loop(self) -> None:
        """Listen for cabin sensor advertisements. Never connects.

        The H5179 carries its readings in the advertisement, so this costs no
        connection slot — which matters because the three pack links already
        hold three, and CoreBluetooth has a finite number. A listener cannot
        disturb them.

        Throttled per sensor: the H5179 advertises every couple of seconds and
        a cabin temperature does not move that fast.
        """
        last: dict[str, int] = {}

        def on_adv(dev, adv) -> None:
            name = self.cfg.cabin_sensors.get(dev.address)
            if name is None:
                return
            reading = decode_ambient(adv.manufacturer_data, adv.service_data)
            if reading is None:
                return
            now = int(time.time())
            if now - last.get(name, 0) < self.cfg.cabin_sample_seconds:
                return
            try:
                self.db.insert_cabin_sample(
                    ts=now,
                    sensor=name,
                    air_temp_f=reading.air_temp_f,
                    humidity_pct=reading.humidity_pct,
                    battery_pct=reading.battery_pct,
                    rssi=getattr(adv, "rssi", None),
                )
            except Exception:
                log.exception("insert_cabin_sample failed")
                return
            last[name] = now

        while not self._stop.is_set():
            try:
                scanner = BleakScanner(detection_callback=on_adv)
                await scanner.start()
                log.info("cabin scanner listening for %d sensor(s)",
                         len(self.cfg.cabin_sensors))
                while not self._stop.is_set():
                    await self._sleep_or_stop(2.0)
                await scanner.stop()
            except Exception as e:
                # Never let a scanner fault take down the pack loops with it —
                # they are the job that matters and they share this event loop.
                log.warning("cabin scanner error: %r", e)
                await self._sleep_or_stop(10.0)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake at once on shutdown.

        A blind asyncio.sleep holds shutdown for its full duration, and the
        longest one here is the parked-vehicle idle poll — the common case.
        Every loop in this module waits through this rather than sleeping
        directly.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _obd_loop(self) -> None:
        """Poll the OBD-II adapter for engine data.

        Shaped like _pack_loop on purpose — same forever-retry with capped
        backoff, same acceptance that being out of range is normal rather than
        an error. The adapter is only reachable with the laptop in the van, and
        only answers PIDs with the ignition on.

        Engine off is the common case and must cost the chassis battery as
        little as possible. The adapter is permanently plugged into the OBD
        port, which is powered from the chassis battery, not the house bank.
        The first version kept the connection open and re-asked every PID every
        30 s. That stops the adapter's own auto-sleep, and every query is
        traffic on the vehicle bus that can keep ECUs awake. So once the engine
        is off this loop disconnects. Every parked_probe_seconds it reconnects
        and asks only ATRV (chassis voltage), which the adapter answers itself
        without touching the bus. An adapter that has gone to sleep stops
        advertising, so those reconnects just fail quietly until the ignition
        wakes it.
        """
        cfg = self.obd_cfg
        assert cfg is not None
        backoff = float(cfg.reconnect_backoff_seconds)
        # Same treatment as the packs, and it will matter more here: with no
        # adapter fitted this loop can only ever fail.
        faillog = _ConnectFailureLog("obd")
        parked = False
        while not self._stop.is_set():
            was_parked = parked
            self._engine_hint.clear()
            try:
                faillog.connecting(cfg.address)
                client = BleakClient(cfg.address)
                # Explicitly bounded rather than `async with`: on macOS a
                # connect to an address CoreBluetooth cannot resolve never
                # returns, and bleak's own timeout does not cover it (verified
                # 2026-09-30 — shutdown hung indefinitely). A TimeoutError here
                # lands in the except below and retries like any other failure.
                await asyncio.wait_for(
                    client.connect(), timeout=cfg.connect_timeout_seconds
                )
                try:
                    faillog.success()
                    chan = _ElmChannel(client, cfg.write_char, cfg.notify_char)
                    await chan.start()
                    parked = await self._obd_session(client, chan, cfg)
                    await chan.stop()
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                backoff = float(cfg.reconnect_backoff_seconds)
                if parked:
                    if not was_parked:
                        log.info("obd: engine off — disconnected, waiting for %s",
                                 "the ACR to see a crank or charging"
                                 if self._acr_live() else
                                 f"a chassis-voltage check every {cfg.parked_probe_seconds} s")
                    await self._obd_wait_parked(cfg)
                    continue
                log.warning("obd: disconnected")
            except Exception as e:
                faillog.failure(e)
                backoff = min(backoff * 1.5, 60.0)
            if self._stop.is_set():
                break
            await self._sleep_or_stop(backoff)

    async def _obd_session(self, client: BleakClient, chan: "_ElmChannel",
                           cfg: cfg_mod.ObdConfig) -> bool:
        """One connection's worth of work. Returns True if the engine is off
        and the caller should disconnect and wait parked_probe_seconds.

        ATRV goes first and before ATZ: it needs no init, and when it reads
        below running_volts the probe is over without a single bus query. If
        the adapter does not implement ATRV (None), fall through and let the
        PIDs decide — the empty-poll counter still parks it within a few
        seconds.
        """
        volts = obd_mod.decode_voltage(
            await chan.ask(obd_mod.CMD_VOLTAGE, timeout=cfg.command_timeout_seconds)
        )
        acr = self._acr_says_engine()
        if acr is False:
            log.debug("obd: ACR mode %s, engine off", self._acr_mode)
            return True
        if acr is None and volts is not None and volts < cfg.running_volts:
            log.debug("obd: chassis %.1f V < %.1f V, engine off",
                      volts, cfg.running_volts)
            return True
        for cmd in obd_mod.INIT_COMMANDS:
            await chan.ask(cmd, timeout=cfg.init_timeout_seconds)
        log.info("obd: adapter initialised (chassis %s V)",
                 "?" if volts is None else f"{volts:.1f}")

        supported = await self._obd_discover(chan, cfg)
        plan = obd_mod.build_plan(supported, cfg.extra)
        decoders = {s.name: obd_mod.compile_formula(s.formula) for s in plan}
        now = int(time.time())
        self.db.upsert_obd_signals(
            now, [(s.ecu, s.name, s.request, s.unit, s.formula) for s in plan]
        )
        log.info("obd: polling %d signals (%s supported standard PIDs)",
                 len(plan), "?" if supported is None else len(supported))
        await self._obd_read_dtcs(chan, cfg)

        header: str | None = None      # None = the default 7DF broadcast
        empty = 0
        cycle = 0
        loop = asyncio.get_running_loop()
        while client.is_connected and not self._stop.is_set():
            t0 = loop.time()
            due = [s for s in plan if cycle % obd_mod.TIER_EVERY[s.tier] == 0]
            replies: dict[str, str] = {}
            for spec in due:
                if spec.header != header:
                    header = await self._obd_set_header(chan, cfg, spec)
                replies[spec.name] = await chan.ask(
                    spec.request, timeout=cfg.command_timeout_seconds
                )
            if header is not None:
                header = await self._obd_set_header(chan, cfg, None)
            # The five core PIDs keep their own narrow table and decoders.
            reading = obd_mod.reading_from_replies({
                pid: replies[attr] for pid, attr in obd_mod.POLL_PLAN
                if attr in replies
            })
            if reading.is_empty:
                # Ignition off, or a dropped cycle. Nothing to store either
                # way: an all-null row would be indistinguishable from a
                # decode failure and would bloat the table across every parked
                # day. Several in a row are needed before parking, so one bad
                # cycle mid-drive does not cost five minutes of RPM.
                empty += 1
                if empty >= cfg.parked_after_empty_polls:
                    return True
            else:
                empty = 0
                self._write_engine_sample(reading)
                ts = int(time.time())
                self.db.insert_obd_samples(ts, [
                    (s.ecu, s.name, replies[s.name],
                     obd_mod.decode_spec(s, replies[s.name], decoders[s.name]))
                    for s in due
                ])
            elapsed = loop.time() - t0
            if cycle == 0:
                # The number to tune tiers against: if a full fast cycle takes
                # longer than poll_seconds the adapter is the bottleneck.
                log.info("obd: first full cycle %d requests in %.2f s",
                         len(due), elapsed)
            cycle += 1
            await self._sleep_or_stop(max(0.0, cfg.poll_seconds - elapsed))
        return False

    async def _obd_discover(self, chan: "_ElmChannel",
                            cfg: cfg_mod.ObdConfig) -> set[str] | None:
        """Ask the supported-PID bitmaps. None if the ECU will not say.

        Each bitmap's last bit says whether the next one exists, so the walk
        stops at the first range the ECU does not cover.
        """
        supported: set[str] = set()
        for base in obd_mod.SUPPORTED_BITMAP_PIDS:
            got = obd_mod.decode_supported(
                base, await chan.ask(base, timeout=cfg.init_timeout_seconds)
            )
            if not got:
                break
            supported |= got
            nxt = f"01{int(base[2:], 16) + 0x20:02X}"
            if nxt not in supported:
                break
        return supported or None

    async def _obd_read_dtcs(self, chan: "_ElmChannel",
                             cfg: cfg_mod.ObdConfig) -> None:
        """Stored (03) and pending (07) trouble codes, once per session.
        Read-only: the clear service (04) is never sent."""
        ts = int(time.time())
        for service in ("03", "07"):
            raw = await chan.ask(service, timeout=cfg.init_timeout_seconds)
            codes = obd_mod.decode_dtcs(raw, service)
            self.db.insert_dtcs(ts, service, codes, raw)
            if codes:
                log.warning("obd: %s trouble codes: %s",
                            "stored" if service == "03" else "pending",
                            ", ".join(codes))

    async def _obd_set_header(self, chan: "_ElmChannel", cfg: cfg_mod.ObdConfig,
                              spec: "obd_mod.RequestSpec | None") -> str | None:
        """Point requests at spec's module, or back at the 7DF broadcast.

        ATCRA narrows which CAN ID the adapter listens to. ATAR restores the
        automatic choice, which is right for standard PIDs.
        """
        t = cfg.command_timeout_seconds
        if spec is None or spec.header is None:
            await chan.ask("ATSH7DF", timeout=t)
            await chan.ask("ATAR", timeout=t)
            return None
        await chan.ask(f"ATSH{spec.header}", timeout=t)
        await chan.ask(f"ATCRA{spec.receive}" if spec.receive else "ATAR", timeout=t)
        return spec.header

    def _write_engine_sample(self, reading: obd_mod.EngineReading) -> None:
        now = int(time.time())
        try:
            self.db.insert_engine_sample(
                ts=now,
                rpm=reading.rpm,
                coolant_c=reading.coolant_c,
                speed_mph=reading.speed_mph,
                engine_load_pct=reading.engine_load_pct,
                runtime_s=reading.runtime_s,
            )
        except Exception:
            log.exception("insert_engine_sample failed")
            return
        log.debug(
            "obd: wrote engine sample (rpm=%s speed=%s coolant=%s load=%s)",
            reading.rpm, reading.speed_mph, reading.coolant_c,
            reading.engine_load_pct,
        )

    async def _pack_loop(self, pack_id: int, addr: str) -> None:
        await self._uart_loop(f"pack {pack_id}", addr,
                              lambda line: self._dispatch(pack_id, line))

    async def _uart_loop(self, label: str, addr: str,
                         on_line: Callable[[str], None],
                         on_connect: Callable[[BleakClient], Awaitable[None]] | None = None,
                         ) -> None:
        """Hold a notify subscription on FFE1 and feed each CRLF line to
        on_line. Shared by the packs and the ACR, which speak the same
        transparent-UART protocol. on_connect, if given, runs as a task for
        the life of each connection and is cancelled when it drops."""
        backoff = float(self.cfg.reconnect_backoff_seconds)
        faillog = _ConnectFailureLog(label)
        while not self._stop.is_set():
            buf = bytearray()

            def on_notify(_handle, data: bytearray) -> None:
                buf.extend(data)
                while b"\r\n" in buf:
                    line, _, rest = buf.partition(b"\r\n")
                    buf[:] = rest
                    on_line(line.decode("ascii", errors="replace"))

            try:
                faillog.connecting(addr)
                client = BleakClient(addr)
                # Explicitly bounded rather than `async with`: a connect to an
                # address CoreBluetooth cannot resolve never returns on macOS
                # and bleak's timeout does not cover it, so a shutdown arriving
                # mid-connect used to hang this process until SIGKILL
                # (diagnosed 2026-09-30 on the OBD loop, same bug here).
                # See BleConfig.connect_timeout_seconds for why 60 s and not 30.
                await asyncio.wait_for(
                    client.connect(),
                    timeout=self.cfg.connect_timeout_seconds,
                )
                try:
                    faillog.success()
                    await client.start_notify(FFE1_CHAR, on_notify)
                    side = (asyncio.create_task(on_connect(client))
                            if on_connect else None)
                    while client.is_connected and not self._stop.is_set():
                        await self._sleep_or_stop(2.0)
                    if side is not None:
                        side.cancel()
                        await asyncio.gather(side, return_exceptions=True)
                    try:
                        await client.stop_notify(FFE1_CHAR)
                    except Exception:
                        pass
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                log.warning("%s: disconnected", label)
                backoff = float(self.cfg.reconnect_backoff_seconds)
            except Exception as e:
                faillog.failure(e)
                backoff = min(backoff * 1.5, 60.0)
            if self._stop.is_set():
                break
            await self._sleep_or_stop(backoff)

    async def _acr_settings_loop(self, client: BleakClient) -> None:
        """Read the ACR's settings with `$LIST` soon after each connect, then
        every acr_settings_seconds. Runs while the connection lasts.

        `$LIST` is the only command ever sent, and it only reads. It goes over
        the connection the sidecar already holds, so nothing has to be paused
        (the ACR takes one connection at a time).
        """
        if self.cfg.acr_settings_seconds <= 0:
            return
        await self._sleep_or_stop(ACR_LIST_FIRST_DELAY_S)
        while client.is_connected and not self._stop.is_set():
            await self._read_acr_settings(client)
            await self._sleep_or_stop(self.cfg.acr_settings_seconds)

    async def _read_acr_settings(self, client: BleakClient) -> None:
        # The reply has no end marker: collect what arrives in a short window.
        # Stream frames keep arriving meanwhile and are handled as usual.
        self._acr_list = []
        try:
            await client.write_gatt_char(FFE1_CHAR, ACR_LIST_CMD)
            await self._sleep_or_stop(ACR_LIST_SETTLE_S)
        except Exception as e:
            log.warning("acr: $LIST failed: %r", e)
            return
        finally:
            lines, self._acr_list = self._acr_list, None
        self._store_acr_settings(lines)

    def _store_acr_settings(self, lines: list[str]) -> None:
        firmware, settings = parse_acr_list(lines)
        if not settings:
            log.warning("acr: no settings in the $LIST reply %r", lines)
            return
        now = int(time.time())
        prev = self.db.latest_acr_settings()
        if not self.db.insert_acr_settings(now, firmware, settings,
                                           "\n".join(l.strip() for l in lines)):
            return
        log.info("acr: settings %s", settings)
        if prev is not None and prev[2] != settings:
            old = prev[2]
            changed = {k: [old.get(k), settings.get(k)]
                       for k in sorted(set(old) | set(settings))
                       if old.get(k) != settings.get(k)}
            log.warning("acr: settings changed since %s: %s", prev[0], changed)
            self.db.insert_event(Event(now, "acr_settings_changed", Severity.INFO,
                                       {"changed": changed, "firmware": firmware}))
        self._check_acr_expect(now, settings)

    def _check_acr_expect(self, now: int, settings: dict) -> None:
        """Raise acr_settings_unexpected on the read that first differs from
        ble.acr_expect, and clear it on the first that matches again."""
        if not self.cfg.acr_expect:
            return
        off = {k: [want, settings.get(k)] for k, want in self.cfg.acr_expect.items()
               if settings.get(k) != want}
        last = self.db.conn.execute(
            "SELECT type FROM events WHERE type IN "
            "('acr_settings_unexpected', 'acr_settings_unexpected_cleared') "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        raised = last is not None and last[0] == "acr_settings_unexpected"
        if off and not raised:
            log.warning("acr: settings differ from ble.acr_expect: %s", off)
            self.db.insert_event(Event(now, "acr_settings_unexpected", Severity.WARN,
                                       {"off": off}))
        elif not off and raised:
            self.db.insert_event(Event(now, "acr_settings_unexpected_cleared",
                                       Severity.INFO, {}))

    def _dispatch_acr(self, line: str) -> None:
        frame = parse_acr_frame(line)
        if frame is None and self._acr_list is not None and line.strip():
            self._acr_list.append(line)
            return
        if frame is None:
            # Outside a `$LIST` reply nothing else is expected. Logged once in
            # full, then quietly, in case it means something.
            if line.strip():
                if not self._acr_unknown_warned:
                    log.info("acr: unrecognised line %r", line)
                    self._acr_unknown_warned = True
                else:
                    log.debug("acr: unrecognised line %r", line)
            return
        now = int(time.time())
        self._note_acr_mode(frame.get("field1"))
        changed = self._acr_last is None or any(
            frame.get(k) != self._acr_last.get(k) for k in ACR_CHANGE_FIELDS)
        if not changed and now - self._acr_last_write_ts < self.cfg.acr_sample_seconds:
            return
        if changed and self._acr_last is not None:
            # DEBUG, not INFO: if field 1 or 2 turns out to be a current it
            # changes every frame for a whole drive.
            log.debug("acr: %s -> %s", self._acr_last["raw"], frame["raw"])
        if self.db.insert_acr_sample(ts=now, **frame):
            self._acr_last = frame
            self._acr_last_write_ts = now

    def _note_acr_mode(self, mode: int | None) -> None:
        prev, self._acr_mode = self._acr_mode, mode
        self._acr_seen_ts = time.monotonic()
        if mode in ACR_ENGINE_MODES and prev not in ACR_ENGINE_MODES:
            self._engine_hint.set()

    def _acr_live(self) -> bool:
        return (self._acr_mode is not None
                and time.monotonic() - self._acr_seen_ts <= ACR_FRESH_S)

    def _acr_says_engine(self) -> bool | None:
        """True/False from a live ACR, None when there is no live ACR."""
        if not self._acr_live():
            return None
        return self._acr_mode in ACR_ENGINE_MODES

    async def _hint_or_stop(self, seconds: float) -> bool:
        """Wait for the engine hint, shutdown or the timeout; True on the hint."""
        hint = asyncio.ensure_future(self._engine_hint.wait())
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({hint, stop}, timeout=seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (hint, stop):
                t.cancel()
        return self._engine_hint.is_set() and not self._stop.is_set()

    async def _obd_wait_parked(self, cfg: cfg_mod.ObdConfig) -> None:
        """Wait, disconnected, until the engine may be running.

        With a live ACR: until it changes into an engine mode (a crank), with
        no polling at all. Chassis voltage can't be trusted here, because the
        ACR puts the house bank's voltage on the chassis with the engine off.
        Only a change wakes it, never the mode as it stands, so a mode that
        outlasts the engine can't make the loop reconnect over and over; RPM
        has the last word once connected. The hint is cleared as each session
        starts, so a crank during the session or the disconnect still wakes
        this. Without a live ACR (none fitted, out of range): after
        parked_probe_seconds, for the voltage probe as before.
        """
        step = min(10.0, float(cfg.parked_probe_seconds))
        waited = 0.0
        while not self._stop.is_set():
            if self._engine_hint.is_set():
                return
            if self._acr_live():
                # Still in an engine mode after RPM said off (an adapter
                # hiccup mid-drive?): retry at the old probe rate, no faster.
                if self._acr_says_engine() and waited >= cfg.parked_probe_seconds:
                    return
                if await self._hint_or_stop(step):
                    return
                waited += step
                continue
            if waited >= cfg.parked_probe_seconds:
                return
            await self._sleep_or_stop(step)
            waited += step

    def _dispatch(self, pack_id_hint: int, line: str) -> None:
        a = parse_frame_a(line)
        if a is not None:
            self._last_a[pack_id_hint] = a
            self._maybe_flush(pack_id_hint)
            return
        b = parse_frame_b(line)
        if b is not None:
            reported = b["pack_id_reported"]
            if reported != pack_id_hint and pack_id_hint not in self._mismatch_warned:
                log.warning(
                    "pack %d: BMS reports pack_id=%d (BLE address mapped to %d); "
                    "trusting BMS-reported id",
                    pack_id_hint, reported, pack_id_hint,
                )
                self._mismatch_warned.add(pack_id_hint)
            # Always key by the BMS-reported id — it's authoritative.
            self._last_b[reported] = b
            self._maybe_flush(reported)

    def _maybe_flush(self, pack_id: int) -> None:
        now = int(time.time())
        last = self._last_write_ts.get(pack_id, 0)
        if now - last < self.cfg.sample_downsample_seconds:
            return
        a = self._last_a.get(pack_id)
        b = self._last_b.get(pack_id)
        if not a:
            return  # need at least Frame A
        cells_mv = [cv * 10 for cv in a["cells_cv"]]
        self.db.insert_cell_samples(now, pack_id, cells_mv)
        self.db.insert_pack_ble_sample(
            ts=now,
            pack_id=pack_id,
            pack_voltage_cv=a["pack_v_cv"],
            soc_pct=a["soc_pct"],
            current_a=a["current_a"],
            temp_dc=b["temp_dc"] if b else None,
            big_counter=b["big_counter"] if b else None,
            field_a5=a["field5"],
            field_a6=a["field6"],
            flags_hex=a["flags_hex"],
            config_hex=b["config_hex"] if b else None,
        )
        self._last_write_ts[pack_id] = now
        log.debug(
            "pack %d: wrote ble sample (cells_mv=%s soc=%d V=%.2f I=%d)",
            pack_id, cells_mv, a["soc_pct"], a["pack_v_cv"] / 100, a["current_a"],
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="cerberus.ble")
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    setup_logging(args.log_level)

    # Like the collector, wait rather than exit on a box not set up yet (or
    # with BLE off): exiting would only make systemd/launchd restart it in a
    # loop. Starts by itself once the settings change into something usable.
    from .__main__ import reexec, wait_for_config
    try:
        cfg = cfg_mod.load(args.config)
        reason = None if cfg.ble.enabled else cfg_mod.ConfigError("ble.enabled is false")
        waiting = None if cfg.ble.enabled else (
            "Bluetooth is off; waiting until it is turned on "
            "(Settings > Find Bluetooth devices)")
    except (cfg_mod.ConfigError, FileNotFoundError, yaml.YAMLError) as e:
        reason, waiting = e, None
    if reason is not None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while wait_for_config(args.config, reason, stop=stop, waiting=waiting):
            try:
                if cfg_mod.load(args.config).ble.enabled:
                    reexec(args.config, module="cerberus.ble")
            except Exception as err:          # a half-written change: keep waiting
                reason = err
        return 0

    db = Db(cfg.storage.sqlite_path)
    collector = BleCollector(cfg.ble, db, obd_cfg=cfg.obd)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, collector.shutdown)
        log.info("cerberus.ble starting (%d packs%s%s)",
                 len(cfg.ble.pack_addresses),
                 ", acr" if cfg.ble.acr_address else "",
                 ", obd" if cfg.obd.enabled else "")
        try:
            await collector.run()
        finally:
            db.close()
            log.info("cerberus.ble stopped")

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
