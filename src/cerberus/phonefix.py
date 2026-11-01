"""The owner's phone as the van's position, while it is with the van.

Most vans have no GPS on the GX (this one doesn't); the owner's phone does.
The web app, when the owner turns it on for that phone, sends the phone's
position (browser geolocation) to the UI, which decides whether it can stand
for the van's, and if so writes it to a small file beside config.yaml. The
collector picks that up into location.VanLocation as source "phone".

**Only when the phone is with the van.** The owner checking in from home over
Tailscale must not move a van parked in storage. A fix counts when:

* the request comes from the van's own network (the box itself, or its LAN);
  or
* it comes over Tailscale, but Tailscale reaches the phone directly on the
  box's own network (its peer address is on one of the box's LANs): the
  phone is on the van's WiFi and merely used the Tailscale address. This is
  the common case once a phone has the HTTPS address bookmarked; or
* it comes over Tailscale while the van is moving: engine charging current
  in the live snapshot, or engine RPM from OBD, within the last few minutes.

and the fix is fresh and reasonably accurate. Anything else is refused with
the reason, and nothing is written.

**Who is asking.** The peer address alone isn't enough: `tailscale serve`
(how the UI gets HTTPS, which browsers require before they share location)
connects from localhost, so every remote phone would look like the box.
client_kind() treats a loopback peer as the box only when the Host header
names localhost and no proxy headers are present; the same test guards
setting the first settings PIN.

Fixes stay on the box: only the weather request carries coordinates off it,
as it already did.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

PHONE_FILE = ".cerberus-phone-fix.json"
MAX_FIX_AGE_S = 120          # a fix older than this, by the phone's clock, is refused
MAX_ACCURACY_M = 1000.0      # coarser than this isn't "where the van is"
MOVING_WINDOW_S = 300        # engine activity within this counts as "moving"
SNAPSHOT_FRESH_S = 60

BOX, LAN, TAILSCALE = "box", "lan", "tailscale"
# Over Tailscale, but on the van's network (see tailnet_peer and
# ui.access.peer_on_lan).
TAILSCALE_LOCAL = "tailscale_local"
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
_PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login",
                  "Tailscale-User-Name", "Tailscale-Headers-Info")


def _host_name(host_header: str | None) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):                       # [::1]:8088
        return h[: h.find("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def client_kind(addr: str, headers) -> str:
    """BOX (a browser on the box itself), LAN (the van's network) or
    TAILSCALE (remote, directly or through `tailscale serve`)."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return TAILSCALE                        # unknown: the cautious answer
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        proxied = any(headers.get(h) for h in _PROXY_HEADERS)
        if proxied or _host_name(headers.get("Host")) not in _LOCAL_HOSTS:
            return TAILSCALE
        return BOX
    if ip in _TAILNET_V4 or ip in _TAILNET_V6:
        return TAILSCALE
    return LAN


def tailnet_peer(addr: str, headers) -> str | None:
    """The requesting device's Tailscale address, if the request came over
    Tailscale: the peer itself, or through `tailscale serve`, the client it
    names in X-Forwarded-For."""
    candidates = [addr]
    fwd = headers.get("X-Forwarded-For") or ""
    if fwd:
        candidates.insert(0, fwd.split(",")[0].strip())
    for c in candidates:
        try:
            ip = ipaddress.ip_address(c.split("%")[0])
        except ValueError:
            continue
        if ip in _TAILNET_V4 or ip in _TAILNET_V6:
            return str(ip)
    return None


def moving(conn: sqlite3.Connection, now: int, charge_on_a: float) -> str | None:
    """Why the van looks to be moving right now, or None."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = 'live.snapshot'").fetchone()
        snap = json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError):
        snap = {}
    bank, solar, ac = snap.get("bank") or {}, snap.get("solar") or {}, snap.get("ac") or {}
    if snap.get("ts") and now - int(snap["ts"]) <= SNAPSHOT_FRESH_S and bank.get("current") is not None:
        engine_a = bank["current"] - (solar.get("charge_a") or 0.0)
        if engine_a > charge_on_a and not ac.get("charging_from_ac"):
            return f"the engine is charging the batteries ({engine_a:.0f} A)"
    try:
        hit = conn.execute("SELECT MAX(ts) FROM engine_samples WHERE rpm > 0 AND ts >= ?",
                           (now - MOVING_WINDOW_S,)).fetchone()
    except sqlite3.Error:
        hit = None
    if hit and hit[0]:
        return "the engine is running"
    return None


def parse_fix(body: dict, now: int) -> dict:
    """{lat, lon, accuracy_m, ts} from the web app, or ValueError."""
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
        acc = float(body.get("accuracy_m") or 0.0)
        ts = int(float(body.get("ts_ms") or now * 1000) / 1000)
    except (KeyError, TypeError, ValueError):
        raise ValueError("expected lat, lon, accuracy_m and ts_ms") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise ValueError("position out of range")
    return {"lat": round(lat, 5), "lon": round(lon, 5), "accuracy_m": round(acc), "ts": ts}


def judge(kind: str, fix: dict, now: int, moving_why: str | None) -> tuple[bool, str]:
    """Whether the fix can stand for the van's position, and why (not)."""
    if abs(now - fix["ts"]) > MAX_FIX_AGE_S:
        return False, "That position is out of date; the phone will send a fresh one."
    if fix["accuracy_m"] > MAX_ACCURACY_M:
        return False, (f"That position is only accurate to {fix['accuracy_m'] / 1000:.1f} km; "
                       "waiting for a better fix.")
    if kind in (BOX, LAN):
        return True, "This phone is on the van's network, so it's with the van."
    if kind == TAILSCALE_LOCAL:
        return True, ("This phone reaches the box directly on the van's network "
                      "(through Tailscale), so it's with the van.")
    if moving_why:
        return True, f"The van is moving ({moving_why}), so the phone is taken to be with it."
    return False, ("Not used: this phone is connected from outside the van's network and "
                   "the van isn't moving, so it may not be with the van.")


def path_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(PHONE_FILE)


def write(config_path: str | Path, fix: dict, kind: str) -> None:
    p = path_for(config_path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".phone-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({**fix, "via": kind, "written": int(time.time())}, f)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(config_path: str | Path) -> dict | None:
    try:
        d = json.loads(path_for(config_path).read_text())
        return {"lat": float(d["lat"]), "lon": float(d["lon"]), "ts": int(d["ts"]),
                "accuracy_m": d.get("accuracy_m"), "via": d.get("via")}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
