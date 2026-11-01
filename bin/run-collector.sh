#!/bin/bash
# Launcher for the Cerberus collector under launchd.
#
# Exists so the Cerbo password stays in .env alone. launchd plists live in
# ~/Library/LaunchAgents and get read by anything that can read the home
# directory, so putting CERBERUS_MQTT_PASSWORD in the plist's
# EnvironmentVariables would copy the secret into a second, less protected
# place. This mirrors the EnvironmentFile= approach in systemd/cerberus.service.
#
# launchd handles restart-on-exit (KeepAlive) and stdout/stderr redirection
# (StandardOutPath), so this script does neither — it just builds the
# environment and hands off via exec, keeping the PID launchd supervises the
# same as the Python process.
set -euo pipefail

# The deployment: .env, config.yaml, soak/ and .venv. Defaults to this
# checkout. Set CERBERUS_HOME to keep a van's own files in a folder of their
# own and run this checkout's code with them.
HERE="${CERBERUS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HERE"

# No .env, or no password in it, is a box not set up yet: the collector then
# waits for first-run setup in the UI (which writes both) rather than exiting
# into a launchd restart loop. It also reads .env itself, so a password set
# later reaches it without this script running again.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Keep writing to the soak DB so history stays continuous across the switch
# from the hand-launched process to launchd.
export CERBERUS_SQLITE_PATH="${CERBERUS_SQLITE_PATH:-$HERE/soak/data/cerberus.db}"

# Left empty deliberately: an empty value means "fall back to config.yaml",
# which carries the mDNS failover list. A stale value inherited from the
# environment would pin the collector to one host.
export CERBERUS_MQTT_HOST="${CERBERUS_MQTT_HOST:-}"

# Hold a power assertion for the collector's lifetime.
#
# Measured 2026-08-08: with the default `pmset sleep 1`, this Mac slept after a
# minute idle on battery and DarkWoke ~every 15 minutes for ~20s. The collector
# was blind for 7.7 of 8 hours (96% data loss) — every gap matched a sleep/wake
# pair exactly. launchd cannot help: the process is suspended, not dead.
#
#   -i  prevent idle system sleep (this is the one that matters on battery)
#   -s  prevent system sleep while on AC power
#
# caffeinate runs the collector as its child and exits with it, so launchd
# still supervises the right lifetime and KeepAlive still works.
#
# Costs van battery — this keeps a laptop awake 24/7 off the same bank
# Cerberus is monitoring. Set CERBERUS_PREVENT_SLEEP=0 to opt out without
# editing this file. Note lid-close sleeps regardless of any assertion, and
# the real fix is the 12V mini PC (see launchd/README.md).
CMD=("$HERE/.venv/bin/python" -m cerberus
     --config "$HERE/config.yaml"
     --log-level "${CERBERUS_LOG_LEVEL:-INFO}")

if [[ "${CERBERUS_PREVENT_SLEEP:-1}" != "0" ]] && command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i -s "${CMD[@]}"
fi

exec "${CMD[@]}"
