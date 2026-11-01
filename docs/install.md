# Installing Cerberus on a van's mini PC

Cerberus
runs on a small Linux PC in the van, on the same network as the Victron Cerbo
GX. It only reads from the Cerbo; it never changes anything on it.

This van's own setup (a Mac under launchd) is in [launchd/README.md](../launchd/README.md).

## 1. Hardware

- **A mini PC that runs Debian 12 or Ubuntu 24.04** (Python 3.11 or newer),
  with Bluetooth if the batteries or cabin sensor are read over BLE.
- **Power from the house bank on its own fused circuit.** Its 12 V input must
  cover the bank's whole range: about 10 V empty to **14.75 V**, the peak this
  van has seen while driving. A PC with a narrower input needs a regulated
  12 V supply in front of it.
- **Heat.** The cabinet the electronics live in reached **52 °C (125 °F)** in
  summer with the system off. Pick a PC rated for that (fanless industrial
  models usually are), or vent the cabinet.
- **Network:** the van's router (Starlink, or a travel router such as a
  GL.iNet), the same network the Cerbo is on. Wired if it's near the router.

## 2. Prepare the Cerbo

On the GX (Remote Console or the touch screen):

1. **Set a Remote Console password** (Settings > General, the access level /
   security settings). Cerberus connects with it; it is the only thing the
   setup wizard asks for.
2. **Turn on MQTT on LAN** (Settings > Services, or Settings > Integrations on
   newer firmware). Cerberus reads everything through it.

## 3. Install

On the mini PC, from a checkout of this repository:

```sh
git clone https://github.com/kishg/Cerberus-Core.git
cd Cerberus/cerberus
sudo bin/install-linux.sh
```

The script installs the OS packages it needs, a `cerberus` service user,
the package in `/opt/cerberus/.venv`, and three systemd services:

| Service | What it does |
|---|---|
| `cerberus` | The collector: reads the Cerbo, stores history, runs the detectors |
| `cerberus-ui` | The web app, on port 8088 |
| `cerberus-ble` | Optional BLE readings (Lithionics packs, ACR, cabin sensor, OBD) |

Settings live in `/etc/cerberus` and the database in `/var/lib/cerberus`. No
configuration is installed: until setup is done the collector and BLE
services wait (they don't crash-loop), and the web app shows **Set up
Cerberus**.

Re-running the script upgrades the code and keeps settings and data.

## 4. Set the settings PIN

Anyone on the van's network can look at Cerberus; changing settings needs a
PIN. The first PIN can only be set on the box itself:

```sh
sudo -u cerberus /opt/cerberus/.venv/bin/python -m cerberus.ui \
  --config /etc/cerberus/config.yaml --set-pin
```

(The same command changes it later. PINs are 4-12 digits, stored only as a
hash.)

## 5. Set up

From a phone or laptop on the van's network, open `http://<mini PC>:8088`
(the install script prints the address). Under **Set up Cerberus**:

1. **Find the Cerbo.** It searches the network; or type the Cerbo's address.
2. **Connect** with the Cerbo's Remote Console password. It is checked on the
   Cerbo and stored only in `/etc/cerberus/.env`, readable by the service
   user alone, never in a configuration file.
3. **Check what was detected** (battery packs or monitor, capacity, system
   voltage, time zone) and add the home ZIP code if the Cerbo has no GPS. Then
   **Finish setup**. Live data appears on **Now** within a minute.

Everything else is under **Settings**. Some values are learned once the van
has run for a while (panel voltage, engine charging current, a hot joint on
the charging path); Settings shows "learning" until then.

## 6. Remote access and HTTPS (optional)

[Tailscale](https://tailscale.com) reaches the van from anywhere, including
through Starlink, with no ports opened and no Cerberus account.

1. Install Tailscale on the mini PC and your phone (tailscale.com/download),
   then on the mini PC: `sudo tailscale up`.
2. In the Tailscale admin console, under DNS, enable **MagicDNS** and
   **HTTPS certificates**.
3. On the mini PC: `sudo tailscale serve --bg 8088`. Cerberus is then at
   `https://<mini PC name>.<tailnet>.ts.net/` from any of your devices.
   (`sudo tailscale serve --https=443 off` turns it off.)

HTTPS is what lets a phone share its location. For a van without GPS on the
Cerbo, open Cerberus on your phone at the `https://` address, sign in, and
tick **Use this phone's location for the van** under Settings. It is used
only while the phone is with the van: on the van's network, or while driving.

## 7. Home Assistant (optional)

Cerberus doesn't need Home Assistant. If the van runs it, turn on **Publish to
Home Assistant** under Settings (with the broker's address and port); readings
and events then appear as HA entities.

## 8. BLE batteries and sensors

Reading Lithionics packs, the ACR or a Govee cabin sensor over Bluetooth is
not in the wizard yet: add the device addresses under `ble:` in
`/etc/cerberus/config.yaml` (see this van's [config.yaml](../config.yaml) for
the shape) and set `ble.enabled: true`. The BLE service starts by itself
once it is on.

## Moving an existing Cerberus to a new box

For a van already running Cerberus elsewhere (this van: a Mac, moving to its
mini PC). After step 3 on the new box, and with SSH to it (Tailscale works),
run on the **old** box:

```sh
bin/migrate-to-box.sh you@new-box
```

It copies the settings, the Cerbo password, the PIN, the last phone position,
the full history and the drive reports, takes the database copy while the old
collector keeps running, installs it all on the new box (asking for sudo
there), and deletes the copy on both ends. It refuses a box that is already
set up unless `--force`. Two things change on purpose:

- **Bluetooth is off** with no devices: addresses differ between machines
  (macOS UUIDs vs Linux MACs). On the new box, near the van, run
  **Settings > Find Bluetooth devices > Scan**, then **Use selected**.
- **Home Assistant publishing is off**: turn it on in Settings with the
  broker's address if HA is reachable from the new box.

Sign in again on the new box's address. Then let both run and compare them:

```sh
.venv/bin/python tools/compare_boxes.py http://<old box>:8088 http://<new box>:8088 --minutes 30
```

"AGREE" means the new box reads the van the same way; then stop the old
box's services.

## Updating, logs, removing

- **Update:** `git pull` in the checkout, then `sudo bin/install-linux.sh`.
- **Logs:** `journalctl -u cerberus -u cerberus-ui -u cerberus-ble -f`.
  "waiting for first-run setup" before setup is normal.
- **Remove:** `sudo systemctl disable --now cerberus cerberus-ui cerberus-ble`,
  then delete `/opt/cerberus`, and `/etc/cerberus` and `/var/lib/cerberus`
  if the settings and history should go too.

## Beta notes

Vans with a Victron Cerbo GX need nothing beyond the above. Before handing a
van back, check:

- [ ] Now shows live battery, solar and AC readings.
- [ ] Health shows the Cerbo and weather as fresh.
- [ ] The time on Now matches the van's local time.
- [ ] Settings shows every detected value matching (or knowingly changed).
- [ ] The box survives a power cycle: services come back on their own.
- [ ] The owner has the PIN and, if they want remote access, Tailscale.

What to send back from each van: the Cerbo's firmware version, the battery
make and pack count, and `journalctl -u cerberus --since "-1 day"` if anything
looked wrong.
