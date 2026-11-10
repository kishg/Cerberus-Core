#!/bin/bash
# Install a bundle from `python -m cerberus.migrate bundle` on the new box.
# Run as root there; bin/migrate-to-box.sh copies the bundle over and runs it:
#
#   sudo bin/install-bundle.sh /tmp/cerberus-migrate.123 [--no-systemd] [--force]
#
# Stops the services, puts settings in /etc/cerberus and the database and
# drive reports in /var/lib/cerberus (owned by the cerberus user, secrets
# 0600), and starts the services again.
set -euo pipefail

SRC="${1:?usage: $0 BUNDLE_DIR [--no-systemd]}"
SYSTEMD=1
FORCE=0
for a in "${@:2}"; do
  case "$a" in
    --no-systemd) SYSTEMD=0 ;;
    --force) FORCE=1 ;;
    *) echo "install-bundle: unknown option $a" >&2; exit 64 ;;
  esac
done
ETC=/etc/cerberus
STATE=/var/lib/cerberus
[[ $EUID -eq 0 ]] || { echo "install-bundle: run as root" >&2; exit 77; }
[[ -f "$SRC/MANIFEST.json" ]] || { echo "install-bundle: $SRC is not a bundle" >&2; exit 66; }
id cerberus >/dev/null 2>&1 || { echo "install-bundle: run bin/install-linux.sh first" >&2; exit 69; }
if [[ $FORCE -eq 0 && -e "$ETC/config.yaml" ]]; then
  echo "install-bundle: this box is already set up; --force replaces its settings and history" >&2
  exit 73
fi

if [[ $SYSTEMD -eq 1 ]]; then
  systemctl stop cerberus-ble cerberus-ui cerberus 2>/dev/null || true
fi

install -d -o cerberus -g cerberus -m 750 "$ETC" "$STATE"
for f in config.yaml setup.yaml; do
  install -o cerberus -g cerberus -m 640 "$SRC/$f" "$ETC/$f"
done
for f in .env .cerberus-ui-pin.json .cerberus-phone-fix.json .cerberus-notes.json; do
  if [[ -e "$SRC/$f" ]]; then install -o cerberus -g cerberus -m 600 "$SRC/$f" "$ETC/$f"; fi
done
if [[ -d "$SRC/.cerberus-victron" ]]; then
  rm -rf "$ETC/.cerberus-victron"
  cp -r "$SRC/.cerberus-victron" "$ETC/.cerberus-victron"
  chown -R cerberus:cerberus "$ETC/.cerberus-victron"
  chmod -R go-rwx "$ETC/.cerberus-victron"
fi
# A stale write-ahead log beside the new database would be replayed into it.
rm -f "$STATE/cerberus.db-wal" "$STATE/cerberus.db-shm"
install -o cerberus -g cerberus -m 640 "$SRC/cerberus.db" "$STATE/cerberus.db"
if [[ -d "$SRC/reports" ]]; then
  rm -rf "$STATE/reports"
  cp -r "$SRC/reports" "$STATE/reports"
  chown -R cerberus:cerberus "$STATE/reports"
fi

if [[ $SYSTEMD -eq 1 ]]; then
  systemctl start cerberus cerberus-ui cerberus-ble
  sleep 5
  systemctl is-active cerberus cerberus-ui cerberus-ble
fi
echo "install-bundle: installed"
