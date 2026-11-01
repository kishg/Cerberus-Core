#!/bin/bash
# Install Cerberus on a Debian or Ubuntu mini PC. Run as root from a checkout:
#
#   sudo bin/install-linux.sh
#
# Installs (and re-installs, keeping settings and data):
#   /opt/cerberus/.venv     the package, in its own virtualenv
#   /etc/cerberus           settings, owned by the cerberus user, because the
#                           UI writes there (first-run setup writes config.yaml
#                           and the Cerbo password in .env; Settings writes
#                           setup.yaml; the PIN hash and phone position)
#   /var/lib/cerberus       the database (the collector's StateDirectory)
#   systemd units           cerberus (collector), cerberus-ui, cerberus-ble
#
# No config.yaml is installed: on a new box the collector and the BLE sidecar
# wait, and the UI shows first-run setup at http://<box>:8088. An existing
# /etc/cerberus is never touched.
#
# Options:
#   --no-apt       don't install OS packages (python3, venv, bluez)
#   --no-systemd   don't install or start units (containers, tests)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/cerberus
ETC=/etc/cerberus
STATE=/var/lib/cerberus
APT=1
SYSTEMD=1
for arg in "$@"; do
  case "$arg" in
    --no-apt) APT=0 ;;
    --no-systemd) SYSTEMD=0 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "install: unknown option $arg" >&2; exit 64 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "install: run as root (sudo $0)" >&2
  exit 77
fi

if [[ $APT -eq 1 ]]; then
  say "OS packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q python3 python3-venv bluez
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
  echo "install: Cerberus needs Python 3.11 or newer (found $(python3 --version))" >&2
  exit 69
}

say "Service user"
if ! id cerberus >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin cerberus
fi
if getent group bluetooth >/dev/null; then
  usermod -aG bluetooth cerberus
fi

say "Package (into $PREFIX/.venv)"
install -d -m 755 "$PREFIX"
if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
  python3 -m venv "$PREFIX/.venv"
fi
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
# Non-editable: the install doesn't depend on this checkout staying put.
"$PREFIX/.venv/bin/pip" install -q --upgrade "$SRC[ble]"

say "Settings and data directories"
install -d -o cerberus -g cerberus -m 750 "$ETC" "$STATE"

if [[ $SYSTEMD -eq 1 ]]; then
  say "systemd units"
  for unit in cerberus.service cerberus-ui.service cerberus-ble.service; do
    install -m 644 "$SRC/systemd/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl enable --now cerberus.service cerberus-ui.service cerberus-ble.service
  # A re-install runs the new code.
  systemctl restart cerberus.service cerberus-ui.service cerberus-ble.service
fi

"$PREFIX/.venv/bin/python" -c 'import cerberus.ui.server, cerberus.firstrun, cerberus.ble' \
  || { echo "install: the installed package doesn't import" >&2; exit 70; }

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Installed"
cat <<EOF
Next:
  1. Set the settings PIN, on this box:
       sudo -u cerberus $PREFIX/.venv/bin/python -m cerberus.ui --config $ETC/config.yaml --set-pin
  2. Open http://${ip:-<this box>}:8088 from a phone or laptop on the van's
     network and follow "Set up Cerberus" (finds the Cerbo, asks its password).
  3. Optional: remote access and HTTPS with Tailscale (docs/install.md).
EOF
