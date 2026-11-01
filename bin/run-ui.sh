#!/bin/bash
# Launcher for the Cerberus UI under launchd (or by hand).
#
# Needs no secrets: the UI reads config.yaml and the database, read-only, and
# never talks to MQTT. It points at the same database as run-collector.sh so
# the two agree; launchd handles restart-on-exit and logging, so this script
# only sets the environment and hands off via exec.
set -euo pipefail

# The deployment: .env, config.yaml, soak/ and .venv. Defaults to this
# checkout. Set CERBERUS_HOME to keep a van's own files in a folder of their
# own and run this checkout's code with them.
HERE="${CERBERUS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$HERE"

export CERBERUS_SQLITE_PATH="${CERBERUS_SQLITE_PATH:-$HERE/soak/data/cerberus.db}"

exec "$HERE/.venv/bin/python" -m cerberus.ui \
  --config "$HERE/config.yaml" \
  --log-level "${CERBERUS_LOG_LEVEL:-INFO}"
