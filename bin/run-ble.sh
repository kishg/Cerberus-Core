#!/bin/bash
# Launcher for the Cerberus BLE sidecar under launchd.
#
# Mirrors bin/run-collector.sh: the Cerbo password stays in .env rather than in
# the plist, because ~/Library/LaunchAgents is readable by anything that can
# read the home directory. The sidecar does not speak MQTT at all, but
# config.load() requires CERBERUS_MQTT_PASSWORD before it will return a Config,
# so .env is a hard dependency here too.
#
# Why this exists: the sidecar last wrote a sample on 2026-08-29 and nothing
# noticed for three weeks. It had no launchd job — only the collector did — so
# whatever stopped it, nothing ever brought it back. Running it by hand would
# reproduce exactly that failure. KeepAlive is the actual fix.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Like run-collector.sh: no .env (a box not set up yet) is not an error;
# the sidecar waits for setup instead of exiting into a launchd restart loop.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# The same soak database the collector writes. Two writers is deliberate and
# handled: Db sets busy_timeout=5000 precisely because the sidecar and the
# collector share this file. Without this export the sidecar would fall back to
# config.yaml's storage.sqlite_path, which is the *container* path
# /data/cerberus.db — it would silently create an orphan database and the
# samples would never appear next to the electrical data.
export CERBERUS_SQLITE_PATH="${CERBERUS_SQLITE_PATH:-$HERE/soak/data/cerberus.db}"

# No caffeinate here, unlike run-collector.sh. Sleep would stop BLE capture
# just as dead, but the collector already holds a system-wide power assertion
# for its own lifetime and a second one buys nothing. If the collector is ever
# run with CERBERUS_PREVENT_SLEEP=0, this sidecar sleeps with it.
exec "$HERE/.venv/bin/python" -m cerberus.ble \
  --config "$HERE/config.yaml" \
  --log-level "${CERBERUS_BLE_LOG_LEVEL:-INFO}"
