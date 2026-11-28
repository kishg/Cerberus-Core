"""Where to open Cerberus from, and who else can reach the box.

* **Van network:** http://<LAN address>:<port>, for anything on the van's
  network. The only address Settings offers: remote access is a Cerberus
  Cloud feature, and an owner's own VPN is their setup, not Cerberus's
  (Core #52). So Settings no longer offers a Tailscale address.
* **Support access (Core #48):** when the box is on the support provider's
  tailnet (``support.tailnet`` in config), Settings says plainly that support
  access is on, who has it, and how to end it. It never shows the tailnet or
  the box's address on it (Core #52). Owner-visible support access is a rule of the
  plan, not a nicety. The UI never logs Tailscale in or out.

Tailscale is read only through ``tailscale status --json`` (read-only), cached
for a minute. No Tailscale, or not logged in, means nothing to show.
``peer_on_lan`` stays for phone location until the rest of #52.
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


def tailnet(status: dict | None) -> dict | None:
    """{tailnet, name} the box is logged in to, or None."""
    if not status or status.get("BackendState") != "Running":
        return None
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    current = status.get("CurrentTailnet") or {}
    net = str(current.get("Name") or "").strip()
    if not net and name.count(".") >= 3:
        net = name.split(".", 1)[1]                   # box.tail1234.ts.net -> tail1234.ts.net
    if not (net or name):
        return None
    return {"tailnet": net or None, "name": name or None}


def support(on: dict | None, cfg) -> dict | None:
    """Support access, when the box is on the configured support tailnet; else None."""
    want = str(getattr(cfg, "tailnet", "") or "").strip().lower() if cfg else ""
    if not on or not want:
        return None
    here = {str(on.get("tailnet") or "").lower(), str(on.get("name") or "").lower()}
    if not any(h == want or h.endswith("." + want) for h in here if h):
        return None
    return {"on": True, "provider": getattr(cfg, "provider", "") or None,
            "contact": getattr(cfg, "contact", "") or None,
            "end": "Ask the provider to end it, or run `sudo tailscale logout` on the box."}


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


def status(now: float | None = None) -> dict | None:
    """`tailscale status --json`, cached for a minute; None without Tailscale."""
    global _cache
    now = time.monotonic() if now is None else now
    if _cache and now - _cache[0] < CACHE_S:
        return _cache[2]
    ts = _tailscale()
    out = _json([ts, "status", "--json"]) if ts else None
    _cache = (now, 0, out)
    return out


def remote(support_cfg=None, now: float | None = None) -> dict:
    """Who can reach this box remotely: its tailnet, and support access if configured."""
    on = tailnet(status(now))
    return {"tailnet": on, "support": support(on, support_cfg)}


def view(port: int, now: float | None = None, support_cfg=None) -> dict:
    """What Settings shows: the van-network address and support access. Never the tailnet the
    box is on or its name there: beta boxes carry the support provider's tailnet, and that address is
    not the owner's way in (Core #52). Health still reports it (``remote``)."""
    return {"lan": lan_url(port), "support": remote(support_cfg, now)["support"]}
