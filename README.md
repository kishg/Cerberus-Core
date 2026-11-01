# Cerberus

Battery-intelligence service for a Lithionics + Victron Cerbo GX van
system. Read-only MQTT client: ingests the Cerbo's live data, logs samples
to SQLite, runs cross-pack analysis the Cerbo doesn't do (SoC drift,
full-charge calibration, current-sharing), and republishes findings to MQTT
for Home Assistant.

See `../cerberus-implementation.md` for the full spec — this is M1
(collector core) only.

## Dev (macOS)

Mac and Cerbo on the same LAN.

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
cp .env.example .env   # then put the Cerbo Remote Console password in CERBERUS_MQTT_PASSWORD
export $(grep -v '^#' .env | xargs)
python -m cerberus --config config.yaml --log-level INFO
```

Verify against the live broker:

```sh
mosquitto_sub -h 192.168.1.84 -t 'N/#' -v \
  -u '0123456789ab' -P "$CERBERUS_MQTT_PASSWORD"
```

## Tests

```sh
pytest -q
```

Fixtures `tests/fixtures/drift_discharging.jsonl` (62/64/25 — spread 39) and
`tests/fixtures/near_full_postdrive.jsonl` (96/98/61 — spread 37) come from
real Cerbo captures and are the M2 detector acceptance gate.

## Prod (Debian mini PC)

```sh
sudo bin/install-linux.sh
```

Installs the collector, the web UI and the BLE sidecar as systemd services
under a `cerberus` user; the UI then walks through first-run setup. Full
steps, Tailscale and the beta checklist: [docs/install.md](docs/install.md).

`docker/` holds the older collector-only image (it bakes in a config.yaml and
has no UI); the install script is the supported path.

## M1 scope

- Connect (auth) + keepalive + auto-reconnect
- Parse `system/0/Batteries` → BankState; map uc token → pack 1/2/3
- Throttled SQLite writes (WAL); one row per pack per 30s
- Heartbeat liveness (offline if no heartbeat >60s)
- Container or systemd

M2 = detectors + HA discovery output. M3 = resilience + optional sync.

## Hard rules

- Read-only to Victron: subscribe `N/`, publish only `R/<portal>/keepalive`.
  Never publish `W/`.
- SoC comes only from `system/0/Batteries`.
- No per-cell data over MQTT — no cell-imbalance detector.
- Password from `CERBERUS_MQTT_PASSWORD` env, never config or git.
- Pack identity keys off `uc` token, not array position or instance.

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
