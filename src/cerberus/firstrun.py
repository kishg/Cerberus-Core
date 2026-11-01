"""First-run setup: find the Cerbo, check its password, write the first config.

A new box has no config.yaml. The UI's setup wizard drives this module:

1. **discover()** lists GX devices on the box's own networks: the mDNS name
   ``venus.local``, plus every host on the box's /24s with the MQTT port
   open. A host counts as a Victron GX when its web page says so (the GX
   serves a Victron-branded login page; Home Assistant's broker on the same
   LAN does not).
2. **probe()** connects with the owner's password, reads the portal ID from
   ``N/+/system/0/Serial`` (the GX announces it unasked) and collects the
   same inventory the collector records (cerberus.detect), so the wizard can
   propose settings before anything is written. It is read-only: it
   subscribes, and sends only the ``R/<portal>/keepalive`` read request the
   collector itself sends. The GX checks the password alone (any username
   works, verified 2026-10-31), so no username is asked for.
3. **complete()** validates the whole result first, then writes the
   password to .env, a new config.yaml, and the chosen settings to
   setup.yaml. The collector, waiting for a config, starts on its own.

The password lives in .env only (cerberus.envfile), never in config.yaml.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import socket
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as cfg_mod
from . import detect as detect_mod
from . import envfile
from . import settings as settings_mod
from .location import VanLocation

MQTT_PORT = 1883
MDNS_NAME = "venus.local"
PROBE_TIMEOUT_S = 12.0
# After the Batteries array arrives, wait this long for Capacity and the
# other per-instance topics, which trail it.
SETTLE_S = 2.5


class SetupError(Exception):
    pass


# ------------------------------------------------------------------- discovery

def local_networks() -> list[ipaddress.IPv4Network]:
    """The /24s of this box's own IPv4 addresses (not loopback, not the
    Tailscale range, which is not where a van's GX lives)."""
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))        # no packet is sent; picks the LAN source address
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    nets = set()
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip.is_loopback or ip.is_link_local or ip in cgnat:
            continue
        nets.add(ipaddress.ip_network(f"{a}/24", strict=False))
    return sorted(nets)


def port_open(host: str, port: int = MQTT_PORT, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def looks_like_gx(host: str, timeout: float = 2.0) -> bool:
    """The GX's web server answers with a Victron-branded page."""
    try:
        with urllib.request.urlopen(f"http://{host}/", timeout=timeout) as r:
            return b"victron" in r.read(65536).lower()
    except (OSError, ValueError):
        return False


def resolve(name: str) -> str | None:
    try:
        return socket.getaddrinfo(name, MQTT_PORT, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


def discover(networks=None, port_check=port_open, gx_check=looks_like_gx,
             resolver=resolve) -> list[dict]:
    """GX candidates, most likely first: {host, address, gx}."""
    found: dict[str, dict] = {}
    mdns = resolver(MDNS_NAME)
    if mdns and port_check(mdns):
        found[mdns] = {"host": MDNS_NAME, "address": mdns}
    nets = local_networks() if networks is None else networks
    hosts = [str(h) for n in nets for h in n.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for h, ok in zip(hosts, pool.map(port_check, hosts)):
            if ok and h not in found:
                found[h] = {"host": h, "address": h}
    out = list(found.values())
    for c in out:
        c["gx"] = gx_check(c["address"])
    out.sort(key=lambda c: (not c["gx"], c["host"] != MDNS_NAME, c["address"]))
    return out


# ----------------------------------------------------------------------- probe

class Probe:
    """What one connection to the GX hears, folded into an inventory. Kept
    apart from the network code so it can be tested on recorded messages."""

    def __init__(self):
        self.portal_id: str | None = None
        self.batteries: list | None = None
        self.batteries_ts: float | None = None
        self.pack_health: dict[int, dict[str, float]] = {}
        self.location = VanLocation()

    def message(self, topic: str, payload: bytes, now: float | None = None) -> None:
        try:
            value = json.loads(payload).get("value") if payload else None
        except (ValueError, AttributeError):
            return
        parts = topic.split("/")
        if topic.endswith("/system/0/Serial") and value:
            self.portal_id = str(value)
        elif topic.endswith("/system/0/Batteries") and isinstance(value, list):
            self.batteries = value
            self.batteries_ts = time.monotonic() if now is None else now
        elif len(parts) >= 4 and parts[2] == "battery" and parts[-1] == "Capacity":
            try:
                self.pack_health.setdefault(int(parts[3]), {})["capacity_ah"] = float(value)
            except (TypeError, ValueError):
                pass
        else:
            self.location.update(topic, value)

    def done(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.batteries_ts is not None and now - self.batteries_ts >= SETTLE_S

    def result(self) -> dict:
        inv = detect_mod.inventory(int(time.time()), self.batteries or [],
                                   self.pack_health, self.location)
        props, notes = detect_mod.proposals(inv)
        for key, prop in props.items():
            st = settings_mod.BY_KEY[key]
            prop.update(label=st.label, unit=st.unit)
        return {"portal_id": self.portal_id, "inventory": inv,
                "proposals": props, "notes": notes}


def probe(address: str, password: str, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """Connect to the GX with the owner's password and see what it reports."""
    import paho.mqtt.client as mqtt

    p = Probe()
    state: dict = {}
    connected = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"cerberus-setup-{int(time.time())}")
    c.username_pw_set("cerberus-setup", password)

    def on_connect(cl, _u, _f, rc, _p=None):
        state["rc"] = rc
        connected.set()
        if not rc.is_failure:
            cl.subscribe("N/+/system/0/Serial")

    def on_message(cl, _u, msg):
        had = p.portal_id
        p.message(msg.topic, msg.payload)
        if p.portal_id and not had:
            pid = p.portal_id
            cl.subscribe([(f"N/{pid}/system/0/Batteries", 0),
                          (f"N/{pid}/battery/+/Capacity", 0),
                          (f"N/{pid}/settings/0/Settings/System/TimeZone", 0),
                          (f"N/{pid}/gps/+/Fix", 0),
                          (f"N/{pid}/gps/+/Position/#", 0)])
            cl.publish(f"R/{pid}/keepalive", b"")

    c.on_connect, c.on_message = on_connect, on_message
    try:
        c.connect(address, MQTT_PORT, keepalive=30)
    except OSError as err:
        raise SetupError(f"Couldn't reach {address}: {err}") from None
    c.loop_start()
    try:
        if not connected.wait(min(timeout, 8)):
            raise SetupError(f"{address} didn't answer as an MQTT broker.")
        rc = state["rc"]
        if rc.is_failure:
            if "authori" in str(rc).lower() or "password" in str(rc).lower():
                raise SetupError("Wrong password. Use the Cerbo's Remote Console password "
                                 "(Settings > General > Access level / Security on the GX).")
            raise SetupError(f"The Cerbo refused the connection: {rc}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not p.done():
            time.sleep(0.2)
    finally:
        c.loop_stop()
        c.disconnect()
    if not p.portal_id:
        raise SetupError(f"{address} accepted the password but didn't identify itself as a "
                         "Victron GX (no portal ID). Check MQTT on LAN is enabled on the GX.")
    return p.result()


# -------------------------------------------------------------------- complete

def _config_text(hosts: list[str], portal_id: str, sqlite_path: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    host_lines = "\n".join(f"    - {h}" for h in hosts)
    return f"""# Cerberus configuration, written by first-run setup on {stamp}.
#
# Edit freely: the Settings screen writes its changes to setup.yaml beside
# this file and never rewrites this one. The Cerbo password is in .env, not
# here. Everything not listed uses its default.

mqtt:
  hosts:                         # tried in order
{host_lines}
  port: 1883
  portal_id: {portal_id}
  username: {portal_id}          # password: CERBERUS_MQTT_PASSWORD in .env

storage:
  sqlite_path: {sqlite_path}
"""


def complete(config_path: str | Path, address: str, host: str, password: str,
             portal_id: str, changes: dict, sqlite_path: str) -> None:
    """Write the first configuration. Validates everything before writing
    anything; refuses on a box that is already set up."""
    config_path = Path(config_path)
    if cfg_mod.is_set_up(config_path):
        raise SetupError("This box is already set up.")
    if config_path.exists():
        raise SetupError(f"{config_path.name} exists but has no Cerbo connection; add its "
                         "mqtt section by hand, or move it aside and run setup again.")
    try:
        envfile.check_value(password)
    except envfile.EnvFileError as err:
        raise SetupError(f"Password: {err}") from None
    # The address that answered first (fastest, and right on this network),
    # then the name it was found by, then mDNS, which follows the GX onto
    # another network (a Starlink LAN) where the address will differ.
    hosts = list(dict.fromkeys([address, host, MDNS_NAME]))
    text = _config_text(hosts, portal_id, sqlite_path)
    import yaml
    base = yaml.safe_load(text)
    setup = settings_mod.build_setup(base, {}, changes, password=password)
    # config.yaml last: its existence is what makes the box "set up", so the
    # waiting collector never starts on half of it.
    settings_mod.write_setup(config_path, setup)
    envfile.set_value(envfile.path_for(config_path), envfile.MQTT_PASSWORD, password)
    config_path.write_text(text)


def change_password(config_path: str | Path, password: str) -> None:
    try:
        envfile.set_value(envfile.path_for(config_path), envfile.MQTT_PASSWORD, password)
    except envfile.EnvFileError as err:
        raise SetupError(f"Password: {err}") from None


def connection(config_path: str | Path) -> dict | None:
    """The configured Cerbo connection, for display (no password)."""
    raw = cfg_mod.read_raw(config_path)
    m = raw.get("mqtt") or {}
    if not m.get("portal_id"):
        return None
    hosts = m.get("hosts") or ([m["host"]] if m.get("host") else [])
    return {"portal_id": str(m["portal_id"]), "hosts": [str(h) for h in hosts],
            "password_set": bool(envfile.mqtt_password(config_path))}
