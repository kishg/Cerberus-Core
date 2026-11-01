"""Where to open Cerberus from: the addresses the Settings screen offers to copy.

* **Tailscale HTTPS:** the box's MagicDNS name, when `tailscale serve`
  proxies HTTPS to this UI. It is the address a phone needs for location
  sharing (browsers share location only over HTTPS), and it works from
  anywhere on the owner's tailnet.
* **Van network:** http://<LAN address>:<port>, for anything on the van's
  network.

Read from the Tailscale CLI (`tailscale status --json` and `tailscale serve
status --json`, both read-only), cached for a minute. No Tailscale, or not
logged in, simply means no Tailscale address.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
import time

from ..firstrun import local_networks

CACHE_S = 60
TIMEOUT_S = 3
# launchd and systemd give services a short PATH; look where Tailscale lives.
_CANDIDATES = ("/usr/local/bin/tailscale", "/usr/bin/tailscale", "/opt/homebrew/bin/tailscale",
               "/Applications/Tailscale.app/Contents/MacOS/Tailscale")

_cache: tuple[float, int, dict] | None = None


def _tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for c in _CANDIDATES:
        if shutil.which(c):
            return c
    return None


def _json(cmd: list[str]) -> dict | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return None


def tailscale_https(port: int, status: dict | None, serve: dict | None) -> dict | None:
    """{name, url, serving} from the two CLI outputs, or None without Tailscale."""
    if not status or status.get("BackendState") != "Running":
        return None
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        return None
    serving = False
    for host, web in ((serve or {}).get("Web") or {}).items():
        if not host.startswith(name):
            continue
        for handler in (web.get("Handlers") or {}).values():
            proxy = str(handler.get("Proxy") or "")
            if proxy.rstrip("/").endswith(f":{port}"):
                serving = True
    return {"name": name, "url": f"https://{name}/" if serving else None, "serving": serving,
            "command": None if serving else f"tailscale serve --bg {port}"}


def peer_on_lan(peer_ip: str, networks=None, status: dict | None = None) -> str | None:
    """Tailscale's direct path to a peer, if it is on one of this box's own
    networks (e.g. "192.168.1.61:41641"): the peer is on the van's network.
    None if relayed, direct over the internet, or unknown."""
    if status is None:
        ts = _tailscale()
        status = _json([ts, "status", "--json"]) if ts else None
    if not status:
        return None
    nets = local_networks() if networks is None else networks
    for peer in (status.get("Peer") or {}).values():
        if peer_ip not in (peer.get("TailscaleIPs") or []):
            continue
        cur = str(peer.get("CurAddr") or "")
        host = cur.rsplit(":", 1)[0].strip("[]") if cur else ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        return cur if any(ip in n for n in nets) else None
    return None


def lan_url(port: int) -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))            # no packet is sent; picks the LAN address
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return None
    return None if ip.startswith("127.") else f"http://{ip}:{port}/"


def view(port: int, now: float | None = None) -> dict:
    global _cache
    now = time.monotonic() if now is None else now
    if _cache and now - _cache[0] < CACHE_S and _cache[1] == port:
        return _cache[2]
    ts = _tailscale()
    tailscale = None
    if ts:
        tailscale = tailscale_https(port, _json([ts, "status", "--json"]),
                                    _json([ts, "serve", "status", "--json"]))
    out = {"tailscale": tailscale, "lan": lan_url(port)}
    _cache = (now, port, out)
    return out
