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
#   /var/log/cerberus       each service's log file, 0750, files 0640, rotated
#                           (cerberus.log); the support file reads them
#   systemd units           cerberus (collector), cerberus-ui, cerberus-ble;
#                           cerberus-wifi (the box's own Wi-Fi network) and
#                           cerberus-gx-port (the GX cabled to the box's
#                           second Ethernet port: an address and DHCP for the
#                           GX alone, no router, no DNS, nothing forwarded)
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
LOGS=/var/log/cerberus
APT=1
SYSTEMD=1
for arg in "$@"; do
  case "$arg" in
    --no-apt) APT=0 ;;
    --no-systemd) SYSTEMD=0 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
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
  # hostapd and dnsmasq-base for the box's own Wi-Fi network (cerberus-wifi, Core #53); nftables for its
  # isolation. dnsmasq-base, not dnsmasq: no system DNS server competing with systemd-resolved.
  apt-get install -y -q python3 python3-venv bluez hostapd dnsmasq-base nftables iw
  # Cerberus runs hostapd itself, with its own settings: the distribution's service stays off.
  systemctl disable --now hostapd.service 2>/dev/null || true
  systemctl mask hostapd.service 2>/dev/null || true
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
# Each service's own log file (cerberus.log), which the support file reads: the UI reads these,
# never the system journal. Made here too, not only by the units' LogsDirectory=, because
# cerberus-wifi runs as root and must find it owned by cerberus.
install -d -o cerberus -g cerberus -m 750 "$LOGS"

if [[ $SYSTEMD -eq 1 ]]; then
  say "systemd units"
  for unit in cerberus.service cerberus-ui.service cerberus-ble.service \
              cerberus-wifi.service cerberus-wifi.path cerberus-wifi-reload.service \
              cerberus-gx-port.service cerberus-gx-port.path cerberus-gx-port-reload.service; do
    install -m 644 "$SRC/systemd/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  # The GX on the second Ethernet port (gx_port.enabled, on by default), before the collector reads its
  # status. On a box with one port, or a network not run by systemd-networkd, it changes nothing and
  # says why (python -m cerberus.gxport status).
  systemctl enable --now cerberus-gx-port.path cerberus-gx-port.service
  systemctl restart cerberus-gx-port.service
  systemctl enable --now cerberus.service cerberus-ui.service cerberus-ble.service
  # A re-install runs the new code.
  systemctl restart cerberus.service cerberus-ui.service cerberus-ble.service
  # The box's own Wi-Fi network: on by default (wifi_ap.enabled), where there's a Wi-Fi card.
  if compgen -G "/sys/class/net/*/wireless" >/dev/null; then
    systemctl enable --now cerberus-wifi.path cerberus-wifi.service
    systemctl restart cerberus-wifi.service
  else
    say "No Wi-Fi card: the box's own network is skipped"
  fi
fi

"$PREFIX/.venv/bin/python" -c 'import cerberus.ui.server, cerberus.firstrun, cerberus.ble' \
  || { echo "install: the installed package doesn't import" >&2; exit 70; }

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Installed"
if [[ -f "$ETC/config.yaml" ]]; then
  # A re-install on a box that's already set up: nothing more to do.
  echo "This box is already set up; it's running the new version."
  echo "Its Wi-Fi label: printed from KNM's console (Cerberus boxes), or here with"
  echo "  sudo $PREFIX/.venv/bin/python -m cerberus.wifiap label --config $ETC/config.yaml --out ~/cerberus-wifi-label.svg"
  exit 0
fi
cat <<EOF
Next:
  1. Set the settings PIN, on this box:
       sudo -u cerberus $PREFIX/.venv/bin/python -m cerberus.ui --config $ETC/config.yaml --set-pin
  2. Open http://${ip:-<this box>}:8088 from a phone or laptop on the van's
     network and follow "Set up Cerberus" (finds the Cerbo, asks its password).
     Recommended: cable the Cerbo to the box's second Ethernet port first; it
     gets 10.43.0.10 there. Keep the Cerbo's own Wi-Fi to the van's router:
     the box never gives it internet (docs/install.md).
  3. Optional: remote access and HTTPS with Tailscale (docs/install.md).
  4. Print the box's Wi-Fi label and stick it on the box: in the van with no
     router, it's how a phone gets in (network name, password, a QR code to
     join and one to open Cerberus). KNM boxes: from the console (Cerberus
     boxes). Otherwise, here:
       sudo $PREFIX/.venv/bin/python -m cerberus.wifiap label \\
         --config $ETC/config.yaml --out ~/cerberus-wifi-label.svg
     Print it at 100 x 62 mm, then delete the file (it holds the password).
EOF
