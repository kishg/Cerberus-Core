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

Hardware Cerberus has run on, and what to avoid: [tested_hardware.md](tested_hardware.md).

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
cd Cerberus-Core
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

## 6. Remote access, and support access

**On the van's network** Cerberus is at `http://<mini PC address>:8088/`
(Settings > Open Cerberus on your phone shows it).

**Away from the van**, remote access is part of a Cerberus Cloud
subscription. Cerberus itself has no remote-access setting. If you run
your own VPN to the van (Tailscale, WireGuard, a VPN on your router), that
is your setup, outside Cerberus; behind Starlink, an overlay VPN such as
Tailscale is the kind that works.

Phone location (for a van without GPS on the Cerbo) needs an HTTPS page,
which the Cloud's remote access provides. It is used only while the phone is
with the van.

**Support access.** If someone supports your box over their tailnet (a beta
box, or a support case), Settings says so plainly, so it is never hidden. Add
the provider to config.yaml:

```yaml
support:
  tailnet: example-support.ts.net   # the provider's tailnet
  provider: Example Support
  contact: help@example.com          # how to reach them to end it
```

While the box is logged in to that tailnet, Settings shows **Support access:
on**, the provider, and how to end it (ask them, or `sudo tailscale logout`
on the box). On any other tailnet, Settings names it. Without a `support:`
section nothing extra is shown. Cerberus only reads `tailscale status`; it
never logs Tailscale in or out.

## 7. Home Assistant (optional)

Cerberus doesn't need Home Assistant. If the van runs it, turn on **Publish to
Home Assistant** under Settings (with the broker's address and port); readings
and events then appear as HA entities.

## 8. BLE batteries and sensors

Lithionics packs, the ACR and Bluetooth thermometers (Govee, Xiaomi,
SwitchBot, BTHome and others) are found from the UI. Signed in, near the
devices, open **Settings > Find Bluetooth devices > Scan** (up to a minute),
tick the ones that are this van's, give each thermometer a name (`cabin`,
`fridge`…), then **Use selected**. That turns Bluetooth on, and the BLE
service starts by itself. Devices already connected stop advertising and
don't show up in a scan; the Bluetooth settings above it list those.

The addresses can also go by hand under `ble:` in `/etc/cerberus/config.yaml`
(the example [config.yaml](../config.yaml) shows the shape), with
`ble.enabled: true`.

## Moving an existing Cerberus to a new box

For a van already running Cerberus elsewhere (this van: a Mac, moving to its
mini PC). After step 3 on the new box, and with SSH to it (Tailscale works),
run on the **old** box:

```sh
bin/migrate-to-box.sh you@new-box
```

If the old box keeps its config, `.env`, venv and database in a folder apart
from the code (`CERBERUS_HOME`, see [launchd/README.md](../launchd/README.md)),
set it here too:
`CERBERUS_HOME=/path/to/deployment bin/migrate-to-box.sh you@new-box`.

It copies the settings, the Cerbo password, the PIN, the last phone position,
the owner's notes on findings, the full history and the drive reports, takes
the database copy while the old collector keeps running, installs it all on
the new box (asking for sudo there), and deletes the copy on both ends. It
refuses a box that is already set up unless `--force`. Two things change on
purpose:

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
- [ ] The owner has the PIN.
- [ ] Settings shows **Support access: on** while the box is on the support tailnet.

What to send back from each van: the Cerbo's firmware version, the battery
make and pack count, and `journalctl -u cerberus --since "-1 day"` if anything
looked wrong.
