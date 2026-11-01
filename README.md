# Cerberus Core

[![tests](https://github.com/kishg/Cerberus-Core/actions/workflows/tests.yml/badge.svg)](https://github.com/kishg/Cerberus-Core/actions/workflows/tests.yml)

Open-source monitoring for the electrical system of a camper van or RV. It
runs on a small computer in the van, records everything the system reports,
and tells the owner in plain words what is wrong, why, and what to check.

- **Collects** live data from a Victron GX (Cerbo) over MQTT: batteries,
  solar, inverter/charger, temperature sensors. It also reads Lithionics
  battery packs and the Lithionics charge relay over Bluetooth, a Govee cabin
  sensor, the local weather, and optionally the engine over an OBD adapter.
- **Stores** it in SQLite on the box, so history stays in the van and
  survives without internet.
- **Finds problems** with deterministic, tested rules. Each finding comes
  with an explanation, advice and evidence: charge-path resistance that ends
  solar charging early, SoC drift without a full charge, alternator
  overvoltage, inverter low-battery alarms from cable drop, uneven pack
  current, cell imbalance, and more.
- **Explains gaps in the data:** the box asleep or off, the GX offline, or
  the van's network down.
- **Has a web UI** for phone and laptop (Now, Findings, History, Health,
  Settings) with first-run setup that detects the system, and notes the owner
  can leave on findings.
- **Home Assistant is optional:** MQTT discovery and state topics, when
  turned on.

**Read-only to Victron.** Cerberus subscribes to the GX's `N/` topics and
publishes only the keepalive. It never writes settings or switches anything.

## Install on a mini PC in the van

A Debian or Ubuntu box on the same network as the GX:

```sh
git clone https://github.com/kishg/Cerberus-Core.git
cd Cerberus-Core
sudo bin/install-linux.sh
```

This installs the collector, the web UI and the Bluetooth sidecar as systemd
services. Then open `http://<box>:8088` and follow first-run setup, which
finds the GX and asks for its Remote Console password. Full steps, remote
access with Tailscale, and moving from another machine are in
[docs/install.md](docs/install.md).

Power it from a regulated 12 V supply on its own fused circuit, never straight
from the battery bank, and keep it out of the hottest cabinet.

## Run on a Mac (development)

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[ble]" pytest
cp .env.example .env    # put the GX's Remote Console password in it
.venv/bin/python -m cerberus --config config.yaml       # collector
.venv/bin/python -m cerberus.ui --config config.yaml    # UI on :8088
```

`launchd/` has the macOS services. Set `CERBERUS_HOME` to keep a van's own
`.env`, `config.yaml`, database and venv in a folder apart from the code (see
[launchd/README.md](launchd/README.md)).

The `config.yaml` here is a worked example with placeholder IDs. On a real
box, first-run setup writes the van's own values.

## Tests

```sh
.venv/bin/python -m pytest -q
```

CI runs them on Ubuntu 24.04 (Python 3.11 to 3.14) and macOS. Several
fixtures are real captures from a van, with identifiers replaced.

## Contributing

Bug reports and pull requests are welcome. Contributions need a signed
contributor agreement first; see [CONTRIBUTING.md](CONTRIBUTING.md). To
report a security issue, see [SECURITY.md](SECURITY.md).

## License

Copyright (C) 2026 KNM Technology.

Cerberus is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See [LICENSE](LICENSE).

The AGPL's network clause (section 13) applies to the web UI: anyone running a
modified Cerberus that others use over a network must offer them its source.
The UI links to it from its footer (`ui.source_url` in config.yaml; point it at
your fork if you run one).
