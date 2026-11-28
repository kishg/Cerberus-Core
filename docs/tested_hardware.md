# Tested hardware

What Cerberus has actually run on, and what we know about each. The
requirements themselves (12 V input range, heat, network) are in
[install.md](install.md#1-hardware). Reports from other hardware are welcome:
open an issue with the model, the OS, and what worked (collector, Bluetooth,
UI), and we'll add it here.

## What it needs

Cerberus is light. Measured on 2026-11-24 on the mini PC below: load average
about 0.01, and its three processes (collector, Bluetooth, UI) use about
115 MB of memory together. The CPU is never what limits the choice. What does:

- **Linux that doesn't sleep.** Debian 12 or Ubuntu 24.04, Python 3.11 or
  newer. A machine that sleeps collects nothing while asleep (see
  [launchd/README.md](../launchd/README.md)).
- **An SSD**, not an SD card or eMMC: the database is written every 30 s, for
  years.
- **Bluetooth that BlueZ supports**, if the packs or sensors are read over
  BLE. Range from inside a cabinet varies by radio; a USB adapter is the
  fallback.
- **12 V power and heat**, as in [install.md](install.md#1-hardware).

## Tested

| Hardware | OS | Status | Notes |
|---|---|---|---|
| **Beelink Mini S13** (Intel N150, 16 GB, 512 GB SATA SSD, Intel AX201 WiFi/Bluetooth, 2x 2.5 GbE) | Ubuntu 24.04 | **Runs Cerberus full time** since 2026-11-20, on a house network. | Not in a van yet, so Bluetooth to the packs and its 12 V supply are still untested. Also runs Home Assistant in Docker with room to spare. Its second Ethernet port is for the Cerbo cable (Core #53); not yet tried with a Cerbo on it. Any Intel N100 / N150 mini PC should behave the same. |
| **MacBook** (Apple silicon) | macOS, under launchd | **Ran in a van**, 2026-08 to 2026-11, now retired. | Works, but loses data whenever the lid closes or it sleeps on battery. Bluetooth gives per-host UUIDs, not MACs, so the config changes when moving to Linux. Fine for development, not for a van. |
| **Victron Cerbo GX** | Venus OS 3.66 (Python 3.12) | **Test suite passes**; never run as the collector. | Storage, surviving firmware updates, and sharing Bluetooth with the GX are all open. |

CI runs the test suite on Ubuntu 24.04 (Python 3.11 to 3.14) and on macOS.

## Untested

- **Raspberry Pi 5.** Should have enough CPU; ARM64 isn't tested. It needs an
  NVMe HAT (not an SD card), a 12 V to 5 V / 5 A supply that doesn't brown
  out, active cooling for a hot cabinet, and the RTC battery so the clock is
  right after a power cut with no network.

## Not suitable

- **Apple TV (tvOS):** no long-running background processes, no Python, no
  USB or serial, and Bluetooth only while the app is on screen.
- **Home Assistant Green:** runs only Home Assistant OS, and has no Bluetooth
  or WiFi.
