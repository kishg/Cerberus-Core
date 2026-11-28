"""The box's own Wi-Fi network (Core #53): how a phone in the van reaches Cerberus with no router and no internet.

A van often has no working network: parked in storage, out of coverage, or with the router or Starlink off
along with the inverter. The box then raises its own network, ``Cerberus-XXXX``, and a phone that joins it
opens the UI at ``http://10.42.0.1:8088``. It is on by default and stays on (decision 22).

- **Isolated.** Phones on it reach the box's UI, DHCP and ping, and nothing else: no other port on the box,
  nothing forwarded to the van's network, the GX or the internet. The rules are their own nftables table,
  ahead of Docker's.
- **Offline by design.** DHCP offers no router and no DNS, so a phone keeps its mobile data for everything
  else (alerts, Cloud features) while it is joined.
- **2.4 GHz.** Intel cards can only start a network there; it reaches across a van.
- **WPA2/WPA3**, falling back to WPA2 alone where the card won't start WPA3. One name and password per box,
  made once and kept in the secret store (``wifi_ssid``, ``wifi_password``).
- **On the box's label.** The network is the way in when the van's router is down, so its name and password
  can't live only behind the UI: ``python -m cerberus.wifiap label`` makes them if needed and draws a label
  with both, a QR code that joins the network and one that opens the UI. Run it once when the box is set up
  and stick the label on the box. The label's password is kept apart (``wifi_password_label``): the UI can
  make a new password, warns that the label then stops working, and can go back to the label's.
- **"With the van".** Its addresses are ordinary local ones, so phonefix counts a phone on it as on the
  van's network, like the box's LAN.

Runs as root under systemd (``cerberus-wifi.service``): ``python -m cerberus.wifiap run --config ...``. It
starts hostapd and dnsmasq as its children, writes what it is doing to ``/run/cerberus-wifi/status.json``
(no password) for the UI, and undoes everything (rules, address, link) when it stops. A settings change
(``wifi_ap`` in setup.yaml) or a new password restarts it (``cerberus-wifi.path``). The UI never needs root.

Hardware checked on the reference box (Intel AX201): docs/install.md, Core #53.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import secrets as _secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .secretstore import SecretStore

log = logging.getLogger(__name__)

SECRET = "wifi_password"
SSID_SECRET = "wifi_ssid"                  # kept, not worked out again: the label must stay right
LABEL_SECRET = "wifi_password_label"       # the password printed on the box's label
TABLE = "cerberus_wifi"
STATUS_DIR = Path("/run/cerberus-wifi")
UI_PORT = 8088
CHANNELS = (1, 6, 11)                 # the 2.4 GHz channels that don't overlap
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"   # nothing that reads as another
PASSWORD_LEN = 12
START_TIMEOUT_S = 15
POLL_S = 30


class WifiError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    channel: int = 6
    interface: str = ""               # empty: the first Wi-Fi interface that can run a network
    address: str = "10.42.0.1/24"     # the box on its own network; phones get .10-.50
    country: str = "US"


def settings_from(raw: dict) -> Settings:
    """``wifi_ap`` in config.yaml / setup.yaml."""
    w = raw.get("wifi_ap") or {}
    if not isinstance(w, dict):
        raise WifiError("wifi_ap must be a mapping")
    enabled = w.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("true", "yes", "on", "1")
    try:
        channel = int(w.get("channel", 6))
    except (TypeError, ValueError):
        raise WifiError("wifi_ap.channel: 1, 6 or 11") from None
    if channel not in CHANNELS:
        raise WifiError("wifi_ap.channel: 1, 6 or 11")
    address = str(w.get("address", Settings.address))
    try:
        net = ipaddress.ip_interface(address)
    except ValueError:
        raise WifiError(f"wifi_ap.address: {address!r} isn't an address like 10.42.0.1/24") from None
    if net.version != 4 or not net.ip.is_private or net.network.prefixlen != 24:
        raise WifiError("wifi_ap.address: a private IPv4 address with /24, like 10.42.0.1/24")
    iface = str(w.get("interface") or "")
    if iface and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface):
        raise WifiError("wifi_ap.interface: an interface name like wlo1")
    country = str(w.get("country", "US")).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise WifiError("wifi_ap.country: two letters, like US")
    return Settings(enabled=bool(enabled), channel=channel, interface=iface, address=address, country=country)


def ssid(machine_id: str) -> str:
    """Cerberus-XXXX, the last four of the machine ID: stable, and different from the next box's."""
    tail = re.sub(r"[^0-9a-fA-F]", "", machine_id)[-4:].upper()
    return f"Cerberus-{tail or 'BOX'}"


def machine_id(path: str | Path = "/etc/machine-id") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def new_password() -> str:
    return "".join(_secrets.choice(ALPHABET) for _ in range(PASSWORD_LEN))


def _hand_over(store: SecretStore, names: list[str], owner_of: Path | None) -> None:
    """Secrets made by root go to the owner of the settings folder (the user the UI runs as)."""
    if owner_of is not None and os.geteuid() == 0:
        st = owner_of.stat()
        for p in [store.dir] + [store.dir / n for n in names]:
            if p.exists():
                os.chown(p, st.st_uid, st.st_gid)


def identity(store: SecretStore, machine_id_path: str | Path = "/etc/machine-id",
             owner_of: Path | None = None) -> tuple[str, str]:
    """The network's name and password, made once (the first time the label is printed or the network
    starts) and kept: the label printed with them must stay right. The first password is also kept as
    the label's."""
    made = []
    name = store.get(SSID_SECRET)
    if not name:
        name = ssid(machine_id(machine_id_path))
        store.set(SSID_SECRET, name)
        made.append(SSID_SECRET)
    pw = store.get(SECRET)
    if not pw:
        pw = store.get(LABEL_SECRET) or new_password()
        store.set(SECRET, pw)
        made.append(SECRET)
    if not store.get(LABEL_SECRET):
        store.set(LABEL_SECRET, pw)
        made.append(LABEL_SECRET)
    _hand_over(store, made, owner_of)
    return name, pw


def password(store: SecretStore, owner_of: Path | None = None) -> str:
    """The network's password (made with its name if there's none yet)."""
    return identity(store, owner_of=owner_of)[1]


def on_label(store: SecretStore) -> bool:
    """Whether the network still uses the password printed on the box's label."""
    label = store.get(LABEL_SECRET)
    return bool(label) and store.get(SECRET) == label


def use_label_password(store: SecretStore) -> None:
    label = store.get(LABEL_SECRET)
    if not label:
        raise WifiError("this box has no label password to go back to")
    store.set(SECRET, label)


def join_uri(name: str, pw: str) -> str:
    """What a phone's camera reads to join: the Wi-Fi QR format, with its special characters escaped."""
    esc = lambda t: re.sub(r'([\\;,:"])', r"\\\1", t)   # noqa: E731
    return f"WIFI:T:WPA;S:{esc(name)};P:{esc(pw)};;"


def label_svg(name: str, pw: str, url: str) -> str:
    """The box's Wi-Fi label, 100 x 62 mm (a common label-printer size): the name and password printed, a
    QR code that joins the network and one that opens the UI."""
    import segno
    from xml.sax.saxutils import escape as x

    def qr(text, at_x, at_y, scale):
        inner = segno.make(text, error="m").svg_inline(scale=scale, border=1, dark="#000", light="#fff")
        return f'<g transform="translate({at_x},{at_y})">{inner}</g>'

    join = qr(join_uri(name, pw), 40, 150, 9)
    open_ui = qr(url, 690, 150, 8)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="62mm" viewBox="0 0 1000 620">
<rect width="1000" height="620" fill="#fff"/>
<g font-family="Helvetica, Arial, sans-serif" fill="#000">
<text x="40" y="62" font-size="38" font-weight="700" letter-spacing="6">CERBERUS</text>
<text x="40" y="106" font-size="26">Box Wi-Fi: works with no router and no internet</text>
{join}
{open_ui}
<text x="400" y="190" font-size="24">Network</text>
<text x="400" y="232" font-size="34" font-weight="700">{x(name)}</text>
<text x="400" y="290" font-size="24">Password</text>
<text x="400" y="334" font-size="34" font-weight="700" font-family="Menlo, Consolas, monospace">{x(pw)}</text>
<text x="400" y="420" font-size="22">1. Scan the left code to join.</text>
<text x="400" y="452" font-size="22">2. Scan the right code, or open</text>
<text x="400" y="484" font-size="22" font-weight="700">{x(url)}</text>
<text x="40" y="585" font-size="20">Phones keep their mobile data while joined.</text>
</g>
</svg>
"""


def qr_svg(text: str) -> str:
    """A QR code as SVG, drawn on the box."""
    import io
    import segno
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=6, border=2, xmldecl=False, svgns=True,
                                     dark="#000000", light="#ffffff")
    return out.getvalue().decode()


def ap_interface(iw_dev: str, wanted: str = "") -> str | None:
    """The Wi-Fi interface to use: the one named, or the first `iw dev` lists."""
    names = re.findall(r"^\s*Interface\s+(\S+)", iw_dev, re.M)
    if wanted:
        return wanted if wanted in names else None
    return names[0] if names else None


def supports_ap(iw_list: str) -> bool:
    block = re.search(r"Supported interface modes:(.*?)(?:\n\s*Band|\n\S)", iw_list, re.S)
    return bool(block and re.search(r"^\s*\*\s*AP\s*$", block.group(1), re.M))


def hostapd_conf(s: Settings, iface: str, name: str, pw: str, wpa3: bool, ctrl_dir: str) -> str:
    lines = [f"interface={iface}", "driver=nl80211", f"ssid={name}", f"country_code={s.country}", "ieee80211d=1",
             "hw_mode=g", f"channel={s.channel}", "ieee80211n=1", "wmm_enabled=1", "auth_algs=1",
             "wpa=2", "rsn_pairwise=CCMP", f"wpa_passphrase={pw}", f"ctrl_interface={ctrl_dir}"]
    lines += ["wpa_key_mgmt=WPA-PSK SAE", "ieee80211w=1", "sae_require_mfp=1"] if wpa3 else ["wpa_key_mgmt=WPA-PSK"]
    return "\n".join(lines) + "\n"


def nft_ruleset(iface: str, ui_port: int = UI_PORT) -> str:
    """From the box's network: the UI, DHCP and ping, nothing else; nothing forwarded either way. Its own
    table at a priority ahead of the usual filter tables (Docker's), and a drop anywhere is final."""
    i = json.dumps(iface)
    return f"""table inet {TABLE} {{
  chain input {{
    type filter hook input priority -10; policy accept;
    iifname {i} ct state established,related accept
    iifname {i} udp dport 67 accept
    iifname {i} tcp dport {int(ui_port)} accept
    iifname {i} icmp type echo-request accept
    iifname {i} drop
  }}
  chain forward {{
    type filter hook forward priority -10; policy accept;
    iifname {i} drop
    oifname {i} drop
  }}
}}
"""


def dnsmasq_args(s: Settings, iface: str, run_dir: Path) -> list[str]:
    net = ipaddress.ip_interface(s.address).network
    lo, hi = net.network_address + 10, net.network_address + 50
    return ["dnsmasq", "--keep-in-foreground", f"--interface={iface}", "--bind-interfaces",
            "--except-interface=lo", "--port=0", "--no-resolv", "--no-hosts",
            f"--dhcp-range={lo},{hi},{net.netmask},12h",
            "--dhcp-option=3", "--dhcp-option=6",            # no router, no DNS: phones keep their mobile data
            f"--dhcp-leasefile={run_dir / 'leases'}", f"--pid-file={run_dir / 'dnsmasq.pid'}"]


def write_status(status: dict, status_dir: Path = STATUS_DIR) -> None:
    status_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=status_dir, prefix=".status-")
    with os.fdopen(fd, "w") as f:
        json.dump(status, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, status_dir / "status.json")


def read_status(status_dir: Path = STATUS_DIR) -> dict:
    try:
        return json.loads((status_dir / "status.json").read_text())
    except (OSError, ValueError):
        return {"state": "unknown"}


# ------------------------------------------------------------------ running it

class Network:
    """hostapd and dnsmasq for the box's network, the address and the rules; and undoing all of it."""

    def __init__(self, s: Settings, iface: str, name: str, pw: str, run_dir: Path, status_dir: Path = STATUS_DIR,
                 run=subprocess.run, popen=subprocess.Popen):
        self.s, self.iface, self.name, self.pw = s, iface, name, pw
        self.run_dir, self.status_dir = run_dir, status_dir
        self._run, self._popen = run, popen
        self.hostapd = self.dnsmasq = None
        self.security = None
        self.since = None

    def _cmd(self, *args, check=True, input=None):
        return self._run(list(args), check=check, capture_output=True, text=True, input=input)

    def teardown(self) -> None:
        for p in (self.dnsmasq, self.hostapd):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.dnsmasq = self.hostapd = None
        self._cmd("nft", "delete", "table", "inet", TABLE, check=False)
        self._cmd("ip", "addr", "flush", "dev", self.iface, check=False)
        self._cmd("ip", "link", "set", self.iface, "down", check=False)

    def _start_hostapd(self, wpa3: bool) -> bool:
        conf = self.run_dir / "hostapd.conf"
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(hostapd_conf(self.s, self.iface, self.name, self.pw, wpa3, str(self.run_dir / "hostapd")))
        logf = open(self.run_dir / "hostapd.log", "w")
        self.hostapd = self._popen(["hostapd", str(conf)], stdout=logf, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            text = (self.run_dir / "hostapd.log").read_text(errors="replace")
            if "AP-ENABLED" in text:
                return True
            if self.hostapd.poll() is not None or "AP-DISABLED" in text:
                break
            time.sleep(0.25)
        if self.hostapd.poll() is None:
            self.hostapd.terminate()
            self.hostapd.wait(timeout=5)
        self.hostapd = None
        return False

    def start(self) -> None:
        self.teardown()                                  # whatever a crash left behind
        self._cmd("ip", "link", "set", self.iface, "up")
        self._cmd("ip", "addr", "add", self.s.address, "dev", self.iface)
        if self._start_hostapd(wpa3=True):
            self.security = "WPA2/WPA3"
        elif self._start_hostapd(wpa3=False):
            log.warning("the Wi-Fi card wouldn't start WPA3; running WPA2 alone")
            self.security = "WPA2"
        else:
            tail = (self.run_dir / "hostapd.log").read_text(errors="replace").strip().splitlines()[-5:]
            raise WifiError("hostapd couldn't start the network: " + " | ".join(tail))
        self._cmd("nft", "-f", "-", input=nft_ruleset(self.iface))
        self.dnsmasq = self._popen(dnsmasq_args(self.s, self.iface, self.run_dir),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.since = int(time.time())

    def alive(self) -> bool:
        return bool(self.hostapd and self.hostapd.poll() is None and self.dnsmasq and self.dnsmasq.poll() is None)

    def clients(self) -> int:
        out = self._cmd("iw", "dev", self.iface, "station", "dump", check=False).stdout or ""
        return len(re.findall(r"^Station ", out, re.M))

    def status(self) -> dict:
        return {"state": "on", "ssid": self.name, "interface": self.iface,
                "address": str(ipaddress.ip_interface(self.s.address).ip), "channel": self.s.channel,
                "security": self.security, "clients": self.clients(), "since": self.since,
                "updated": int(time.time())}


def run(config_path: str | Path, status_dir: Path = STATUS_DIR, run=subprocess.run, popen=subprocess.Popen,
        machine_id_path: str | Path = "/etc/machine-id", stop=None, poll_s: float = POLL_S) -> int:
    from .config import read_raw
    try:
        s = settings_from(read_raw(config_path))
    except WifiError as err:
        write_status({"state": "error", "error": str(err), "updated": int(time.time())}, status_dir)
        log.error("box Wi-Fi: %s", err)
        return 0                                         # a bad setting: say so, don't restart-loop
    if not s.enabled:
        write_status({"state": "off", "updated": int(time.time())}, status_dir)
        log.info("box Wi-Fi network off (wifi_ap.enabled is false)")
        return 0
    iw_dev = run(["iw", "dev"], capture_output=True, text=True, check=False).stdout or ""
    iface = ap_interface(iw_dev, s.interface)
    iw_list = run(["iw", "list"], capture_output=True, text=True, check=False).stdout or ""
    if not iface or not supports_ap(iw_list):
        why = "no Wi-Fi interface" if not iface else f"{iface} can't run a network (no AP mode)"
        write_status({"state": "unavailable", "error": why, "updated": int(time.time())}, status_dir)
        log.warning("box Wi-Fi network unavailable: %s", why)
        return 0
    store = SecretStore.for_config(config_path)
    name, pw = identity(store, machine_id_path, owner_of=Path(config_path).resolve().parent)
    run_dir = status_dir / "run"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    net = Network(s, iface, name, pw, run_dir, status_dir, run=run, popen=popen)
    stopping = {"now": False}

    def on_signal(*_):
        stopping["now"] = True
    if stop is None:
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        stop = lambda: stopping["now"]  # noqa: E731
    try:
        net.start()
        log.info("box Wi-Fi network up: %s (%s) on %s, channel %d", net.name, net.security, iface, s.channel)
        while not stop():
            if not net.alive():
                raise WifiError("hostapd or dnsmasq stopped")
            write_status(net.status(), status_dir)
            waited = 0.0
            while waited < poll_s and not stop():
                time.sleep(min(0.25, poll_s))
                waited += min(0.25, poll_s)
        write_status({"state": "off", "updated": int(time.time())}, status_dir)
        return 0
    except (WifiError, subprocess.CalledProcessError) as err:
        msg = str(err) if isinstance(err, WifiError) else f"{' '.join(err.cmd)}: {(err.stderr or '').strip()[:200]}"
        write_status({"state": "error", "error": msg, "updated": int(time.time())}, status_dir)
        log.error("box Wi-Fi network: %s", msg)
        return 1                                         # systemd restarts it
    finally:
        net.teardown()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cerberus.wifiap", description="The box's own Wi-Fi network.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the network (as root, under systemd)")
    r.add_argument("--config", required=True)
    lb = sub.add_parser("label", help="draw the box's Wi-Fi label (SVG), making its name and password if needed")
    lb.add_argument("--config", required=True)
    lb.add_argument("--out", required=True, help="where to write the label, e.g. cerberus-wifi-label.svg")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "label":
        from .config import read_raw
        s = settings_from(read_raw(args.config))
        store = SecretStore.for_config(args.config)
        name, _ = identity(store, owner_of=Path(args.config).resolve().parent)
        label_pw = store.get(LABEL_SECRET)
        url = f"http://{ipaddress.ip_interface(s.address).ip}:{UI_PORT}"
        out = Path(args.out)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # it carries the password
        with os.fdopen(fd, "w") as f:
            f.write(label_svg(name, label_pw, url))
        print(f"Label for {name} written to {out}. Print it at 100 x 62 mm and stick it on the box;"
              " then delete the file (it holds the password).")
        return 0
    if args.cmd == "run":
        if os.geteuid() != 0:
            print("run it as root (it is a systemd service: cerberus-wifi)", file=sys.stderr)
            return 1
        return run(args.config)
    return 2


if __name__ == "__main__":
    sys.exit(main())
