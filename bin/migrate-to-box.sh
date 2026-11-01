#!/bin/bash
# Move this Cerberus (config, secrets, PIN, history) onto a new box that
# bin/install-linux.sh has already been run on. Run here, on the old box:
#
#   bin/migrate-to-box.sh user@new-box            # e.g. you@cerberus-van
#   bin/migrate-to-box.sh --force user@new-box    # replace a box already set up
#
# The deployment (config.yaml, .env, .venv, soak/data/cerberus.db) is
# CERBERUS_HOME if set, as for bin/run-*.sh, else this checkout:
#
#   CERBERUS_HOME=~/Cerberus/cerberus bin/migrate-to-box.sh user@new-box
#
# 1. Bundles everything with `python -m cerberus.migrate` into a private
#    temp directory: config.yaml, setup.yaml (Bluetooth cleared, HA off: the
#    new box's addresses differ), .env, the PIN hash, the last phone position,
#    an online copy of the database (the collector here keeps running) and
#    the drive reports.
# 2. Copies it to the box over SSH (Tailscale) and, with sudo there, stops
#    the services, installs it into /etc/cerberus and /var/lib/cerberus owned
#    by the cerberus user, and starts them again.
# 3. Deletes the bundle on both ends: it holds the Cerbo password.
#
# Refuses a box that is already set up unless --force (its settings and
# history would be replaced). The old box keeps running; compare the two with
# tools/compare_boxes.py before retiring it.
set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; shift; fi
TARGET="${1:?usage: $0 [--force] user@host}"
CODE="$(cd "$(dirname "$0")/.." && pwd)"
HERE="${CERBERUS_HOME:-$CODE}"
cd "$HERE"
[[ -f "$HERE/config.yaml" && -x "$HERE/.venv/bin/python" ]] || {
  echo "migrate: no config.yaml and .venv in $HERE; set CERBERUS_HOME to the deployment" >&2
  exit 66
}
DB="${CERBERUS_SQLITE_PATH:-$HERE/soak/data/cerberus.db}"

say() { printf '\n==> %s\n' "$*"; }

say "Checking $TARGET"
ssh "$TARGET" 'test -x /opt/cerberus/.venv/bin/python && test -d /etc/cerberus' || {
  echo "migrate: $TARGET has no Cerberus install; run bin/install-linux.sh there first" >&2
  exit 69
}
if [[ $FORCE -eq 0 ]] && ssh "$TARGET" 'sudo test -e /etc/cerberus/config.yaml'; then
  echo "migrate: $TARGET is already set up; --force replaces its settings and history" >&2
  exit 73
fi

WORK="$(mktemp -d)"
chmod 700 "$WORK"
REMOTE=/tmp/cerberus-migrate.$$
cleanup() {
  rm -rf "$WORK"
  ssh "$TARGET" "sudo rm -rf $REMOTE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "Bundling (the collector here keeps running)"
"$HERE/.venv/bin/python" -m cerberus.migrate bundle --config "$HERE/config.yaml" \
  --db "$DB" --out "$WORK/bundle"

say "Copying to $TARGET"
ssh "$TARGET" "umask 077 && mkdir -p $REMOTE"
cp "$CODE/bin/install-bundle.sh" "$WORK/bundle/"
rsync -a --chmod=go-rwx "$WORK/bundle/" "$TARGET:$REMOTE/"

say "Installing on $TARGET (sudo)"
ssh -t "$TARGET" "sudo bash $REMOTE/install-bundle.sh $REMOTE"

say "Done"
cat <<EOF
On the new box:
  1. Open it (http://<box>:8088, or https://<box>.<tailnet>.ts.net after
     'sudo tailscale serve --bg 8088' there), sign in with the same PIN.
  2. Settings > Find Bluetooth devices > Scan, near the packs: pick the packs,
     ACR and sensors.
  3. Turn on Home Assistant publishing there only if HA is reachable from it.
Then compare the two boxes for a while:
  .venv/bin/python tools/compare_boxes.py http://localhost:8088 http://<box>:8088
EOF
