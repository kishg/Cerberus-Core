"""The GX on the box's second Ethernet port (Core #53, part 2): the recommended install.

On a box with two Ethernet ports, the GX is cabled straight to the box. Collection then doesn't depend
on the van's router; the router only gives the box (and the GX, over its own Wi-Fi) the internet.

- **Its own small network.** The port gets a fixed address on a private /24 (``10.43.0.1/24`` by
  default) and a DHCP server for the GX alone: one address in the pool (``.10``), so the GX's address
  is predictable (``10.43.0.10``) and nothing else cabled there gets one. systemd-networkd does it (a
  ``.network`` file in ``/run/systemd/network``, gone at reboot until this service writes it again):
  no other DHCP server to run. The box also takes a link-local (169.254/16) address there, so a GX
  that fell back to link-local (it does when nothing answers its DHCP) is still reachable.
- **No router and no DNS offered, and nothing routed.** The GX must keep its own internet path (Wi-Fi
  to the van's router) for VRM and firmware updates: owner decision on #53, the GX never depends on
  Cerberus for its internet. The box never forwards or NATs for it.
- **Isolated.** From that port the box accepts only DHCP, ping and the replies to its own connections
  (MQTT to the GX); nothing forwarded either way. Its own nftables table, ahead of Docker's.
- **Safe on any box.** It only takes a physical Ethernet port that nothing else configures
  (systemd-networkd shows it "unmanaged"), and only one: a box with one port (that port is the LAN), a
  box whose network isn't run by systemd-networkd, or one with several free ports (name it in
  ``gx_port.interface``) changes nothing and says why in its status.
- **On by default** (decision 22: beta and bundle boxes ship with the GX on the second port). Settings:
  ``gx_port`` in setup.yaml: ``enabled``, ``interface`` (empty: the one free Ethernet port),
  ``address`` (the box's, /24) and ``dhcp`` (false: link-local only, the escape hatch if a GX would
  route its internet over the gatewayless cable).

Runs as root under systemd (``cerberus-gx-port.service``): ``python -m cerberus.gxport run --config``.
It writes the rules, then the networkd file, then ``/run/cerberus-gx-port/status.json`` (the port, its
link, the GX's address) for the UI, first-run setup and the collector, every 30 s. Stopping the service
leaves the port as it is (a settings change restarts it, and the GX shouldn't drop off each time);
``gx_port.enabled: false`` takes everything down.
"""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import wifiap

log = logging.getLogger(__name__)

TABLE = "cerberus_gx"
STATUS_DIR = Path("/run/cerberus-gx-port")
NETWORK_DIR = Path("/run/systemd/network")
NETWORK_FILE = "20-cerberus-gx-port.network"
SYSFS_NET = Path("/sys/class/net")
GX_OFFSET = 10                          # the GX's address: the network's .10, the pool's only address
LEASE_S = 600                           # short, so a replaced GX gets the address soon
POLL_S = 30
STALE_S = 3 * POLL_S + 30
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")


class GxPortError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    interface: str = ""                 # empty: the one physical Ethernet port nothing else configures
    address: str = "10.43.0.1/24"       # the box on the GX's network
    dhcp: bool = True                   # offer the GX its address (no router, no DNS)

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_interface(self.address).network

    @property
    def box_ip(self) -> str:
        return str(ipaddress.ip_interface(self.address).ip)

    @property
    def gx_address(self) -> str:
        return str(self.network.network_address + GX_OFFSET)


def _bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("true", "yes", "on", "1"):
        return True
    if v in ("false", "no", "off", "0"):
        return False
    raise GxPortError(f"{name}: true or false")


def settings_from(raw: dict) -> Settings:
    """``gx_port`` in config.yaml / setup.yaml."""
    g = raw.get("gx_port") or {}
    if not isinstance(g, dict):
        raise GxPortError("gx_port must be a mapping")
    enabled = _bool(g.get("enabled", True), "gx_port.enabled")
    dhcp = _bool(g.get("dhcp", True), "gx_port.dhcp")
    address = str(g.get("address", Settings.address))
    try:
        net = ipaddress.ip_interface(address)
    except ValueError:
        raise GxPortError(f"gx_port.address: {address!r} isn't an address like 10.43.0.1/24") from None
    if net.version != 4 or not net.ip.is_private or net.ip in LINK_LOCAL or net.network.prefixlen != 24:
        raise GxPortError("gx_port.address: a private IPv4 address with /24, like 10.43.0.1/24")
    if int(net.ip) - int(net.network.network_address) == GX_OFFSET:
        raise GxPortError(f"gx_port.address: .{GX_OFFSET} is the GX's; use another, like .1")
    iface = str(g.get("interface") or "")
    if iface and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface):
        raise GxPortError("gx_port.interface: an interface name like enp1s0")
    try:
        wifi_net = ipaddress.ip_interface(wifiap.settings_from(raw).address).network
    except wifiap.WifiError:
        wifi_net = None                  # its own error, from its own parser
    if wifi_net is not None and wifi_net.overlaps(net.network):
        raise GxPortError(f"gx_port.address: {net.network} is the box Wi-Fi's network; use another, like 10.43.0.1/24")
    return Settings(enabled=enabled, interface=iface, address=address, dhcp=dhcp)


# ------------------------------------------------------------------ what gets written

def network_file(s: Settings, iface: str) -> str:
    """systemd-networkd's config for the port: the box's address, link-local too, and (with dhcp) a
    DHCP server whose only address is the GX's. It offers no router, no DNS and nothing else, so the GX
    keeps its internet on its own Wi-Fi."""
    lines = [
        "# Written by cerberus-gx-port (Cerberus, Core #53): the GX on this port. Don't edit; it is",
        "# rewritten from gx_port in setup.yaml, and gone at reboot until the service writes it again.",
        "[Match]", f"Name={iface}", "",
        "[Link]", "RequiredForOnline=no", "",
        "[Network]",
        "Description=Cerberus: the GX's own cable",
        f"Address={s.address}",
        "ConfigureWithoutCarrier=yes",       # the route stays on this port with no cable: never via the router
        "LinkLocalAddressing=ipv4",          # reaches a GX that fell back to 169.254/16; no IPv6 here
        "IPv6AcceptRA=no",
        "LLMNR=no",
        "MulticastDNS=no",
        f"DHCPServer={'yes' if s.dhcp else 'no'}",
    ]
    if s.dhcp:
        lines += ["", "[DHCPServer]",
                  f"PoolOffset={GX_OFFSET}", "PoolSize=1",
                  "EmitRouter=no", "EmitDNS=no", "EmitNTP=no", "EmitSIP=no", "EmitPOP3=no", "EmitSMTP=no",
                  "EmitLPR=no", "EmitTimezone=no",
                  f"DefaultLeaseTimeSec={LEASE_S}", f"MaxLeaseTimeSec={LEASE_S}"]
    return "\n".join(lines) + "\n"


def nft_ruleset(iface: str, dhcp: bool = True) -> str:
    """From the GX's port: DHCP (if offered), ping, and the replies to the box's own connections; nothing
    else on the box, and nothing forwarded either way. One transaction that replaces the table, so a
    restart never leaves the port open in between. Priority ahead of the usual filter tables (Docker's);
    a drop anywhere is final."""
    i = json.dumps(iface)
    dhcp_rule = f"    iifname {i} udp dport 67 accept\n" if dhcp else ""
    return f"""add table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
  chain input {{
    type filter hook input priority -10; policy accept;
    iifname {i} ct state established,related accept
{dhcp_rule}    iifname {i} icmp type echo-request accept
    iifname {i} drop
  }}
  chain forward {{
    type filter hook forward priority -10; policy accept;
    iifname {i} drop
    oifname {i} drop
  }}
}}
"""


# ------------------------------------------------------------------ which port

def network_file_of(networkctl_status: str) -> str | None:
    """The .network file systemd-networkd uses for a link (None: unmanaged)."""
    m = re.search(r"^\s*Network File:\s*(\S+)", networkctl_status, re.M)
    return None if not m or m.group(1) == "n/a" else m.group(1)


def ports(run=subprocess.run, sysfs: Path = SYSFS_NET) -> list[dict]:
    """The box's physical, wired Ethernet ports, and the networkd file each is configured by."""
    out = []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return out
    for name in names:
        d = sysfs / name
        try:
            wired = (d / "device").exists() and not (d / "wireless").exists() and (d / "type").read_text().strip() == "1"
        except OSError:
            wired = False
        if not wired:
            continue
        st = run(["networkctl", "status", name, "--no-pager"], capture_output=True, text=True, check=False)
        out.append({"name": name, "network_file": network_file_of(st.stdout or "")})
    return out


def choose(s: Settings, found: list[dict]) -> tuple[str | None, str | None]:
    """(interface, None), or (None, why not). Only a port nothing else configures, or the one this
    service configured itself."""
    def free(p):
        return p["network_file"] is None or p["network_file"].endswith("/" + NETWORK_FILE)
    if s.interface:
        p = next((p for p in found if p["name"] == s.interface), None)
        if p is None:
            return None, f"no Ethernet port called {s.interface}"
        if not free(p):
            return None, f"{s.interface} is set up by {p['network_file']}; Cerberus leaves it alone"
        return s.interface, None
    if not found:
        return None, "no Ethernet port"
    ours = [p for p in found if p["network_file"] and p["network_file"].endswith("/" + NETWORK_FILE)]
    if ours:
        return ours[0]["name"], None
    spare = [p for p in found if free(p)]
    if not spare:
        if len(found) == 1:
            return None, f"one Ethernet port ({found[0]['name']}), used for the van's network"
        return None, "no free Ethernet port (each is set up by something else)"
    if len(spare) > 1:
        return None, ("several free Ethernet ports (" + ", ".join(p["name"] for p in spare)
                      + "): set gx_port.interface to the one the GX is cabled to")
    return spare[0]["name"], None


def overlap(s: Settings, iface: str, ip_addr_json: str) -> str | None:
    """Another interface already on the GX port's network: say so (routing would be ambiguous)."""
    try:
        links = json.loads(ip_addr_json or "[]")
    except ValueError:
        return None
    for link in links:
        if link.get("ifname") == iface:
            continue
        for a in link.get("addr_info") or []:
            try:
                net = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
            except (KeyError, ValueError):
                continue
            if net.version == 4 and net.overlaps(s.network):
                return f"{link.get('ifname')} is already on {s.network}; set gx_port.address to another network"
    return None


def leases(networkctl_status: str) -> list[str]:
    """The addresses the port's DHCP server has handed out (``Offered DHCP leases:`` in networkctl)."""
    m = re.search(r"Offered DHCP leases:(.*?)(?:\n\s*[A-Z][A-Za-z ]+:|\Z)", networkctl_status, re.S)
    return re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", m.group(1)) if m else []


def neighbors(ip_neigh_json: str) -> list[str]:
    """IPv4 neighbours that answered on the port (the GX, whatever address it has)."""
    try:
        rows = json.loads(ip_neigh_json or "[]")
    except ValueError:
        return []
    out = []
    for r in rows:
        dst, state = r.get("dst", ""), set(r.get("state") or [])
        if ":" in dst or not dst or state & {"FAILED", "INCOMPLETE"}:
            continue
        out.append(dst)
    return out


def carrier(iface: str, sysfs: Path = SYSFS_NET) -> bool:
    try:
        return (sysfs / iface / "carrier").read_text().strip() == "1"
    except OSError:
        return False


# ------------------------------------------------------------------ status, for the UI and the collector

def read_status(status_dir: Path = STATUS_DIR) -> dict:
    return wifiap.read_status(status_dir)


def write_status(status: dict, status_dir: Path = STATUS_DIR) -> None:
    wifiap.write_status(status, status_dir)


def gx_addresses(status: dict) -> list[str]:
    """Where the GX may be on the port, likeliest first: its lease, the predictable address, neighbours."""
    if status.get("state") != "on":
        return []
    cand = list(status.get("leases") or [])
    if status.get("dhcp") and status.get("gx_address"):
        cand.append(status["gx_address"])
    cand += list(status.get("neighbors") or [])
    return [a for a in dict.fromkeys(str(c) for c in cand) if a != status.get("address")]


def health(status: dict, now: int) -> dict:
    """The port's state for the box's Health page (local only: never in the Cloud health report)."""
    state = status.get("state") if isinstance(status.get("state"), str) else "unknown"
    updated = status.get("updated")
    if state == "on" and (not isinstance(updated, (int, float)) or now - updated > STALE_S):
        state = "stale"
    on = state == "on"
    return {"state": state,
            "interface": status.get("interface") if on else None,
            "link": status.get("link") if on else None,
            "gx_seen": bool(gx_addresses(status)) and status.get("link") == "up" if on else None,
            "devices": len(status.get("neighbors") or []) if on else None,
            "error": status.get("error") if isinstance(status.get("error"), str) else None}


def via(ip: str | None, s: Settings | None = None) -> str | None:
    """Which way the collector reaches the GX: "gx_port" (the box's own cable) or "lan"."""
    s = s or Settings()
    if not ip:
        return None
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return "gx_port" if (a in s.network or a in LINK_LOCAL) else "lan"


def cable_ready(status: dict) -> bool:
    """Whether the GX is really on the cable: the port is set up, the link has carrier, and the GX
    holds its DHCP lease or has answered at its address there. An empty port (no cable, or nothing at
    the other end) is not: the collector would wait on it before failing over to the LAN."""
    gx = status.get("gx_address")
    return (status.get("state") == "on" and status.get("link") == "up" and bool(status.get("dhcp"))
            and bool(gx) and (gx in (status.get("leases") or []) or gx in (status.get("neighbors") or [])))


def with_gx_port(cfg, status: dict):
    """The collector's Cerbo addresses with the GX's address on the port first, but only while the GX
    is really on the cable (cable_ready). Otherwise the configured addresses, unchanged. The LAN
    addresses stay as the fallback either way: the GX on Wi-Fi, through the router.

    Decided when the collector starts. While it runs, plugging the GX in restarts it into the cable
    (Cerberus._config_changed, like a settings change); a pulled cable needs no restart, since the
    MQTT client fails over to the next address on its own."""
    s = getattr(cfg, "gx_port", None)
    if s is None or not s.enabled or not s.dhcp or not cable_ready(status):
        return cfg
    first = str(status["gx_address"])
    hosts = (first,) + tuple(h for h in cfg.mqtt.hosts if h != first)
    if hosts == cfg.mqtt.hosts:
        return cfg
    log.info("the GX's own cable is set up (%s): trying %s first", status.get("interface"), first)
    return dataclasses.replace(cfg, mqtt=dataclasses.replace(cfg.mqtt, hosts=hosts, host=hosts[0]))


# ------------------------------------------------------------------ running it

_notify_socket: str | None = None


def take_notify_socket() -> None:
    """Keep systemd's notify socket for this process only. Left in the environment, the commands this
    service runs (networkctl, systemctl, ...) inherit it and send their own notifications, which
    systemd refuses (NotifyAccess=main) and logs as a warning every poll."""
    global _notify_socket
    _notify_socket = os.environ.pop("NOTIFY_SOCKET", None) or _notify_socket


def notify_ready() -> None:
    """Tell systemd (Type=notify) the status is written, so the collector, ordered after this
    service, reads a status that's there."""
    addr = _notify_socket or os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(b"READY=1")
    except OSError:
        pass


class Port:
    """The port's networkd file and rules; applying and removing them."""

    def __init__(self, s: Settings, iface: str, network_dir: Path = NETWORK_DIR, sysfs: Path = SYSFS_NET,
                 run=subprocess.run):
        self.s, self.iface, self.network_dir, self.sysfs, self._run = s, iface, network_dir, sysfs, run
        self.since: int | None = None

    def _cmd(self, *args, check=True, input=None):
        return self._run(list(args), check=check, capture_output=True, text=True, input=input)

    @property
    def path(self) -> Path:
        return self.network_dir / NETWORK_FILE

    def apply(self) -> bool:
        """Rules first, then the networkd file: the port is never configured without its isolation.
        Returns whether networkd had to be reloaded."""
        self._cmd("nft", "-f", "-", input=nft_ruleset(self.iface, self.s.dhcp))
        text = network_file(self.s, self.iface)
        old = self.path.read_text() if self.path.exists() else None
        if old is not None:
            m = re.search(r"^Name=(\S+)", old, re.M)
            if m and m.group(1) != self.iface:              # moved to another port: undo the old one
                remove(self.network_dir, run=self._run, rules=False)
                old = None
        self.since = int(time.time())
        if old == text:
            return False
        self.network_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)
        self._cmd("networkctl", "reload")
        self._cmd("networkctl", "reconfigure", self.iface, check=False)
        return True

    def status(self) -> dict:
        st = self._cmd("networkctl", "status", self.iface, "--no-pager", check=False).stdout or ""
        neigh = self._cmd("ip", "-j", "-4", "neigh", "show", "dev", self.iface, check=False).stdout or ""
        return {"state": "on", "interface": self.iface, "address": self.s.box_ip,
                "network": str(self.s.network), "dhcp": self.s.dhcp,
                "gx_address": self.s.gx_address if self.s.dhcp else None,
                "link": "up" if carrier(self.iface, self.sysfs) else "down",
                "leases": leases(st) if self.s.dhcp else [], "neighbors": neighbors(neigh),
                "since": self.since, "updated": int(time.time())}


def remove(network_dir: Path = NETWORK_DIR, run=subprocess.run, rules: bool = True) -> bool:
    """Take the port down: the networkd file, its address, and (``rules``) the rules. Whether there was
    anything to remove."""
    path = network_dir / NETWORK_FILE
    had = path.exists()
    if had:
        m = re.search(r"^Name=(\S+)", path.read_text(), re.M)
        path.unlink()
        run(["networkctl", "reload"], check=False, capture_output=True, text=True)
        if m:
            run(["ip", "addr", "flush", "dev", m.group(1)], check=False, capture_output=True, text=True)
    if rules:
        run(["nft", "delete", "table", "inet", TABLE], check=False, capture_output=True, text=True)
    return had


def _active(run, unit: str) -> bool:
    r = run(["systemctl", "is-active", "--quiet", unit], check=False, capture_output=True, text=True)
    return r.returncode == 0


def run(config_path: str | Path, status_dir: Path = STATUS_DIR, network_dir: Path = NETWORK_DIR,
        sysfs: Path = SYSFS_NET, run=subprocess.run, stop=None, poll_s: float = POLL_S, notify=notify_ready) -> int:
    from .config import read_raw

    def final(state: str, why: str | None = None, level=logging.INFO) -> int:
        write_status({"state": state, **({"error": why} if why else {}), "updated": int(time.time())}, status_dir)
        notify()
        if why:
            log.log(level, "GX port %s: %s", state, why)
        return 0

    try:
        s = settings_from(read_raw(config_path))
    except GxPortError as err:
        return final("error", str(err), logging.ERROR)            # a bad setting: say so, don't restart-loop
    if not s.enabled:
        if remove(network_dir, run=run):
            log.info("GX port taken down (gx_port.enabled is false)")
        return final("off")
    if not _active(run, "systemd-networkd"):
        return final("unavailable", "this box's network isn't run by systemd-networkd", logging.WARNING)
    if _active(run, "NetworkManager"):
        return final("unavailable", "NetworkManager runs this box's network; set the port up there",
                     logging.WARNING)
    iface, why = choose(s, ports(run, sysfs))
    if not iface:
        return final("unavailable", why, logging.WARNING)
    addrs = run(["ip", "-j", "-4", "addr", "show"], check=False, capture_output=True, text=True).stdout or ""
    clash = overlap(s, iface, addrs)
    if clash:
        return final("error", clash, logging.ERROR)
    port = Port(s, iface, network_dir, sysfs, run=run)
    stopping = {"now": False}

    def on_signal(*_):
        stopping["now"] = True
    if stop is None:
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        stop = lambda: stopping["now"]  # noqa: E731
    try:
        changed = port.apply()
        log.info("GX port %s: %s/24 for the box%s%s", iface, s.box_ip,
                 f", DHCP for the GX at {s.gx_address} (no router, no DNS)" if s.dhcp else ", link-local only",
                 "" if changed else " (unchanged)")
        write_status(port.status(), status_dir)
        notify()
        while not stop():
            waited = 0.0
            while waited < poll_s and not stop():
                time.sleep(min(0.25, poll_s))
                waited += min(0.25, poll_s)
            if not stop():
                write_status(port.status(), status_dir)
        return 0
    except (OSError, subprocess.CalledProcessError) as err:
        msg = (f"{' '.join(err.cmd)}: {(err.stderr or '').strip()[:200]}"
               if isinstance(err, subprocess.CalledProcessError) else str(err))
        write_status({"state": "error", "error": msg, "updated": int(time.time())}, status_dir)
        notify()
        log.error("GX port: %s", msg)
        return 1                                                   # systemd restarts it


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cerberus.gxport", description="The GX on the box's second Ethernet port.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="set the port up and report on it (as root, under systemd)")
    r.add_argument("--config", required=True)
    sub.add_parser("status", help="print the port's status")
    args = ap.parse_args(argv)
    from .log import setup_logging
    setup_logging("INFO", service="cerberus-gx-port" if args.cmd == "run" else None,
                  fmt="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "status":
        print(json.dumps(read_status(), indent=2))
        return 0
    if os.geteuid() != 0:
        print("run it as root (it is a systemd service: cerberus-gx-port)", file=sys.stderr)
        return 1
    take_notify_socket()
    return run(args.config)


if __name__ == "__main__":
    sys.exit(main())
