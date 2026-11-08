"""The UI's HTTP server: stdlib only, read-only database.

API v1 (the contract every client uses: this web app now, a phone app or the
cloud relay later):

  GET  /api/v1/now                live view (api.now_view)
  GET  /api/v1/history?range=..   charts (api.history_view); 24h | 7d | 30d | all
  GET  /api/v1/findings           findings, open and recently resolved, with owner notes
  GET  /api/v1/health             source freshness, coverage, gaps and causes
  GET  /api/v1/about              license, source link (AGPL-3.0 s.13), device id
  GET  /api/v1/settings           editable settings, values and where they came from,
                                  and what setup detection proposes for each
  GET  /api/v1/setup/detect       what the Cerbo reports, and the proposals (cerberus.detect,
                                  plus settings learned from history, cerberus.learn)
  GET  /api/v1/access             addresses to open Cerberus at (Tailscale HTTPS, van LAN)
  GET  /api/v1/setup/status       set up yet?, the Cerbo connection (never the password)
  POST /api/v1/setup/discover     GX devices on the box's networks       } first-run
  POST /api/v1/setup/probe        {address, password} -> portal ID and   } setup
                                  proposals, read-only                    } (cerberus.
  POST /api/v1/setup/complete     {address, host, password, changes}      }  firstrun);
  POST /api/v1/setup/password     {password}: checked on the Cerbo first  } all need a session
  POST /api/v1/setup/ble/scan     Bluetooth devices nearby, and proposals (cerberus.blescan)
  POST /api/v1/location/phone     {lat, lon, accuracy_m, ts_ms}: this phone's position,
                                  used for the van's only while it is with the van
                                  (cerberus.phonefix); needs a session
  POST /api/v1/settings           {"changes": {key: value}}; needs a session
  POST /api/v1/notes              {key, text, title}: a note on a finding (cerberus.notes);
                                  needs a session
  POST /api/v1/notes/delete       {id}; needs a session
  POST /api/v1/notes/known        {id, known}: mark a note's finding known, or flag it
                                  again; needs a session
  POST /api/v1/auth/login         {"pin": "1234"} -> {"token"}
  POST /api/v1/auth/logout
  POST /api/v1/auth/pin           {"pin"}; sets the first PIN, from the box only

The pre-v1 GET paths (/api/now ...) remain as aliases for open browser tabs.

Static files are served from a fixed allow-list read at startup, never by
joining the request path onto a directory. Every request opens its own
read-only database connection (``mode=ro``): the UI never writes the
database. Its only writes are setup.yaml (cerberus.settings, validated
first) and the PIN file (auth), both beside config.yaml, and during first-run
setup the first config.yaml and the Cerbo password in .env (cerberus.firstrun),
the owner's phone position (cerberus.phonefix), and notes on findings
(cerberus.notes).
Setup's probe is the only time it talks to the Cerbo: one short read-only
MQTT connection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .. import config as cfg_mod
from .. import blescan, detect, firstrun, health, learn, location, notes, phonefix
from .. import settings as settings_mod
from . import access, api, auth

log = logging.getLogger("cerberus.ui")

STATIC = Path(__file__).with_name("static")
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}
MAX_BODY = 64 * 1024
KV_DEVICE_ID = "device.id"
LEARN_CACHE_S = 600


def connect_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(Path(db_path).resolve()))}?mode=ro",
                           uri=True, timeout=5)
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_static() -> dict[str, tuple[bytes, str]]:
    files = {}
    for p in STATIC.iterdir():
        if p.is_file() and p.suffix in TYPES:
            files["/" + p.name] = (p.read_bytes(), TYPES[p.suffix])
    files["/"] = files["/index.html"]
    return files


class App:
    """What a request needs: the database, config-derived context, sessions."""

    def __init__(self, db_path: str, pack_count: int, capacity_ah: float | None,
                 tz_name: str | None, path_limit_mohm: float = 75.0,
                 full_remind_days: float = 30.0, source_url: str = "",
                 config_path: str | None = None, charge_on_a: float = 20.0,
                 def_low_pct: float = 20.0, service_due_mi: float = 500.0,
                 temp_unit: str = "F"):
        self.db_path = db_path
        self.pack_count = pack_count
        self.capacity_ah = capacity_ah
        self.configured_tz = tz_name
        self.path_limit_mohm = path_limit_mohm
        self.full_remind_days = full_remind_days
        self.source_url = source_url
        self.config_path = config_path
        self.charge_on_a = charge_on_a
        self.def_low_pct = def_low_pct
        self.service_due_mi = service_due_mi
        self.temp_unit = temp_unit
        self.sessions = auth.Sessions(config_path) if config_path else None
        self.static = load_static()

    @classmethod
    def from_config(cls, config_path: str, db_path: str) -> "App":
        ui = cfg_mod.load_ui(config_path)
        return cls(db_path, pack_count=ui.pack_count, capacity_ah=ui.capacity_ah,
                   tz_name=ui.timezone, path_limit_mohm=ui.path_limit_mohm,
                   full_remind_days=ui.full_remind_days, source_url=ui.source_url,
                   config_path=config_path, charge_on_a=ui.charge_on_a,
                   def_low_pct=ui.def_low_pct, service_due_mi=ui.service_due_mi,
                   temp_unit=ui.temperature_unit)

    def reload(self) -> None:
        """Pick up a settings change without restarting (sessions survive)."""
        ui = cfg_mod.load_ui(self.config_path)
        self.pack_count, self.capacity_ah = ui.pack_count, ui.capacity_ah
        self.configured_tz, self.path_limit_mohm = ui.timezone, ui.path_limit_mohm
        self.full_remind_days, self.source_url = ui.full_remind_days, ui.source_url
        self.charge_on_a = ui.charge_on_a
        self.def_low_pct = ui.def_low_pct
        self.service_due_mi = ui.service_due_mi
        self.temp_unit = ui.temperature_unit

    def learned(self, conn: sqlite3.Connection, now: int) -> dict:
        """cerberus.learn's proposals, cached: they read 90 days of history
        and move slowly."""
        hit = getattr(self, "_learned", None)
        if hit and now - hit[0] < LEARN_CACHE_S and hit[1] == self.pack_count:
            return hit[2]
        props = learn.proposals(conn, now, self.pack_count)
        self._learned = (now, self.pack_count, props)
        return props

    def context(self, conn: sqlite3.Connection) -> api.Context:
        # Resolved per request: the GX's zone (kv) can change while travelling.
        row = conn.execute("SELECT value FROM kv WHERE key = ?",
                           (location.KV_TIMEZONE,)).fetchone()
        name = self.configured_tz or (row[0] if row else None)
        return api.Context(pack_count=self.pack_count, capacity_ah=self.capacity_ah,
                           tz=location.zone(name), tz_name=name,
                           path_limit_mohm=self.path_limit_mohm,
                           full_remind_days=self.full_remind_days,
                           def_low_pct=self.def_low_pct,
                           service_due_mi=self.service_due_mi,
                           temp_unit=self.temp_unit)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CerberusUI"

        def log_message(self, fmt, *args):     # access log at debug, not stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _route(self, path: str) -> str | None:
            """The v1 endpoint name, accepting the pre-v1 GET aliases."""
            for prefix in ("/api/v1/", "/api/"):
                if path.startswith(prefix):
                    return path[len(prefix):]
            return None

        # ------------------------------------------------------------ GET
        def do_GET(self):
            url = urlsplit(self.path)
            ep = self._route(url.path)
            if ep is not None:
                return self._get_api(ep, url)
            hit = app.static.get(url.path)
            if hit is None:
                return self._json(404, {"error": "not found"})
            return self._send(200, hit[0], hit[1])

        def _detected(self) -> dict:
            """Setup detection; empty if the database can't be read, so the
            settings screen still works without it."""
            try:
                conn = connect_ro(app.db_path)
            except sqlite3.Error:
                return {"inventory": None, "proposals": {}, "notes": []}
            try:
                now = int(time.time())
                view = detect.detect_view(conn, now)
                view["proposals"].update(app.learned(conn, now))
                return view
            except sqlite3.Error:
                log.exception("setup detection failed")
                return {"inventory": None, "proposals": {}, "notes": []}
            finally:
                conn.close()

        def _about(self):
            # Without the database too: a box not set up yet has none.
            device_id = None
            try:
                conn = connect_ro(app.db_path)
                try:
                    row = conn.execute("SELECT value FROM kv WHERE key = ?",
                                       (KV_DEVICE_ID,)).fetchone()
                    device_id = row[0] if row else None
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
            return self._json(200, {
                "license": "AGPL-3.0-or-later", "source_url": app.source_url,
                "device_id": device_id, "api": "v1",
                "pin_set": bool(app.config_path) and auth.has_pin(app.config_path),
                "set_up": self._set_up()})

        def _set_up(self) -> bool:
            return not app.config_path or cfg_mod.is_set_up(app.config_path)

        def _get_api(self, ep, url):
            if ep == "settings":
                return self._settings_view()
            if ep == "setup/status":
                if not app.config_path:
                    return self._json(404, {"error": "setup unavailable"})
                return self._json(200, {"set_up": self._set_up(),
                                        "connection": firstrun.connection(app.config_path),
                                        "pin_set": auth.has_pin(app.config_path)})
            if ep == "setup/detect":
                return self._json(200, self._detected())
            if ep == "about":
                return self._about()
            if ep == "access":
                return self._json(200, access.view(getattr(app, "port", 8088)))
            try:
                conn = connect_ro(app.db_path)
            except sqlite3.Error as err:
                return self._json(503, {"error": f"database unavailable: {err}"})
            try:
                ctx = app.context(conn)
                if ep == "now":
                    return self._json(200, api.now_view(conn, ctx, notes=self._notes()))
                if ep == "chassis":
                    return self._json(200, api.chassis_view(conn, ctx, notes=self._notes()))
                if ep == "health":
                    return self._json(200, health.health_view(conn, int(time.time()), ctx.tz))
                if ep == "findings":
                    return self._json(200, api.findings_view(conn, ctx, notes=self._notes()))
                if ep == "history":
                    rng = parse_qs(url.query).get("range", ["24h"])[0]
                    try:
                        return self._json(200, api.history_view(conn, ctx, rng))
                    except ValueError as err:
                        return self._json(400, {"error": str(err)})
                return self._json(404, {"error": "not found"})
            except sqlite3.Error as err:
                log.exception("api query failed")
                return self._json(503, {"error": f"database error: {err}"})
            finally:
                conn.close()

        def _notes(self) -> list[dict]:
            return notes.read(app.config_path) if app.config_path else []

        def _settings_view(self):
            if not app.config_path:
                return self._json(404, {"error": "settings unavailable"})
            if not self._set_up():
                return self._json(200, {"settings": [], "set_up": False,
                                        "signed_in": app.sessions.valid(self._token())})
            det = self._detected()
            inv = det["inventory"]
            try:
                return self._json(200, {
                    "settings": settings_mod.current(app.config_path, det["proposals"]),
                    "signed_in": app.sessions.valid(self._token()),
                    "detect": {"ts": inv["ts"] if inv else None, "notes": det["notes"]}})
            except cfg_mod.ConfigError as err:
                return self._json(500, {"error": f"configuration does not load: {err}"})

        # ------------------------------------------------------------ POST
        def _token(self) -> str | None:
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.startswith("Bearer ") else None

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if n <= 0 or n > MAX_BODY:
                return None
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        def do_POST(self):
            url = urlsplit(self.path)
            if not url.path.startswith("/api/v1/") or not app.config_path:
                return self._json(404, {"error": "not found"})
            ep = url.path[len("/api/v1/"):]
            body = self._body()
            if body is None:
                return self._json(400, {"error": "expected a JSON object body"})
            addr = self.client_address[0]
            try:
                if ep == "auth/login":
                    return self._json(200, {"token": app.sessions.login(body.get("pin"), addr)})
                if ep == "auth/logout":
                    app.sessions.logout(self._token())
                    return self._json(200, {"ok": True})
                if ep == "auth/pin":
                    # The first PIN only, and only from the box itself; changing
                    # it later is `python -m cerberus.ui --set-pin` on the box.
                    # Not the peer address alone: `tailscale serve` connects
                    # from localhost too (cerberus.phonefix.client_kind).
                    if phonefix.client_kind(addr, self.headers) != phonefix.BOX:
                        return self._json(403, {"error": "set the PIN on the box itself"})
                    if auth.has_pin(app.config_path):
                        return self._json(409, {"error": "a PIN is already set"})
                    auth.set_pin(app.config_path, body.get("pin"))
                    return self._json(200, {"token": app.sessions.login(body.get("pin"), addr)})
                if ep == "settings":
                    if not app.sessions.valid(self._token()):
                        return self._json(401, {"error": "enter the PIN to change settings"})
                    changes = body.get("changes")
                    if not isinstance(changes, dict) or not changes:
                        return self._json(400, {"error": "expected {\"changes\": {...}}"})
                    changes, notice = settings_mod.expand_zip(changes)
                    settings_mod.apply(app.config_path, changes)
                    updated = settings_mod.current(app.config_path,
                                                   self._detected()["proposals"])
                    app.reload()
                    log.info("settings changed from %s: %s", addr, sorted(changes))
                    return self._json(200, {"settings": updated, "restarting": True,
                                            "notice": notice})
                if ep in ("notes", "notes/delete", "notes/known"):
                    if not app.sessions.valid(self._token()):
                        return self._json(401, {"error": "sign in (Settings) to add or delete notes"})
                    return self._notes_post(ep, body, addr)
                if ep == "location/phone":
                    if not app.sessions.valid(self._token()):
                        return self._json(401, {"error": "enter the PIN to share this phone's location"})
                    return self._phone_fix(body, addr)
                if ep.startswith("setup/"):
                    if not app.sessions.valid(self._token()):
                        return self._json(401, {"error": "enter the PIN to set up Cerberus"})
                    return self._setup_post(ep[len("setup/"):], body, addr)
            except auth.AuthError as err:
                return self._json(403, {"error": str(err)})
            except firstrun.SetupError as err:
                return self._json(400, {"error": str(err)})
            except cfg_mod.ConfigError as err:
                return self._json(400, {"error": str(err)})
            except notes.NoteError as err:
                return self._json(400, {"error": str(err)})
            return self._json(404, {"error": "not found"})

        def _notes_post(self, ep: str, body: dict, addr: str):
            if ep == "notes":
                note = notes.add(app.config_path, body.get("key"), body.get("text"),
                                 body.get("title"), known=body.get("known") is True)
                log.info("note added on %s from %s%s", note["key"], addr,
                         " (marked known)" if note["known"] else "")
                return self._json(200, {"note": note})
            if ep == "notes/known":
                if not notes.set_known(app.config_path, body.get("id"), body.get("known") is True):
                    return self._json(404, {"error": "no such note"})
                log.info("note %s known=%s from %s", body.get("id"), body.get("known") is True, addr)
                return self._json(200, {"ok": True})
            if not notes.delete(app.config_path, body.get("id")):
                return self._json(404, {"error": "no such note"})
            log.info("note %s deleted from %s", body.get("id"), addr)
            return self._json(200, {"ok": True})

        def _phone_fix(self, body: dict, addr: str):
            now = int(time.time())
            try:
                fix = phonefix.parse_fix(body, now)
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            kind = phonefix.client_kind(addr, self.headers)
            peer = path = None
            if kind == phonefix.TAILSCALE:
                peer = phonefix.tailnet_peer(addr, self.headers)
                path = access.peer_on_lan(peer) if peer else None
                if path:
                    kind = phonefix.TAILSCALE_LOCAL
            why = None
            if kind == phonefix.TAILSCALE:
                try:
                    conn = connect_ro(app.db_path)
                    try:
                        why = phonefix.moving(conn, now, app.charge_on_a)
                    finally:
                        conn.close()
                except sqlite3.Error:
                    why = None
            ok, reason = phonefix.judge(kind, fix, now, why)
            if ok:
                phonefix.write(app.config_path, fix, kind)
            log.info("phone position from %s (%s%s%s): %s", addr, kind,
                     f", tailnet {peer}" if peer else "", f" direct at {path}" if path else "",
                     "used" if ok else "not used")
            return self._json(200, {"accepted": ok, "reason": reason, "via": kind})

        def _setup_post(self, what: str, body: dict, addr: str):
            password = str(body.get("password") or "")
            if what == "ble/scan":
                log.info("setup: Bluetooth scan for %s", addr)
                try:
                    found = blescan.scan()
                except Exception as err:          # no adapter, no permission, bleak missing
                    return self._json(503, {"error": f"Bluetooth scan failed: {err}"})
                return self._json(200, {"found": found, "proposals": blescan.proposals(found)})
            if what == "discover":
                return self._json(200, {"candidates": firstrun.discover()})
            if what == "probe":
                address = str(body.get("address") or "").strip()
                if not address or not password:
                    return self._json(400, {"error": "enter the Cerbo's address and password"})
                log.info("setup: probing %s for %s", address, addr)
                return self._json(200, firstrun.probe(address, password))
            if what == "complete":
                address = str(body.get("address") or "").strip()
                host = str(body.get("host") or address).strip()
                changes = body.get("changes") or {}
                if not address or not password or not isinstance(changes, dict):
                    return self._json(400, {"error": "address, password and changes are needed"})
                # Probed again here, not taken from the client: the portal ID
                # and password that get written are ones the Cerbo accepted.
                changes, _ = settings_mod.expand_zip(changes)
                portal = firstrun.probe(address, password)["portal_id"]
                firstrun.complete(app.config_path, address, host, password, portal,
                                  changes, str(Path(app.db_path).resolve()))
                app.reload()
                log.info("setup: completed from %s (Cerbo %s at %s)", addr, portal, host)
                return self._json(200, {"ok": True, "portal_id": portal})
            if what == "password":
                conn = firstrun.connection(app.config_path)
                if conn is None:
                    return self._json(409, {"error": "set up the Cerbo connection first"})
                if not password:
                    return self._json(400, {"error": "enter the new password"})
                last = None
                for host in conn["hosts"]:
                    try:
                        firstrun.probe(host, password)
                        break
                    except firstrun.SetupError as err:
                        last = err
                        if "Wrong password" in str(err):
                            raise
                else:
                    raise last or firstrun.SetupError("no Cerbo address configured")
                firstrun.change_password(app.config_path, password)
                log.info("setup: Cerbo password changed from %s", addr)
                return self._json(200, {"ok": True, "restarting": True})
            return self._json(404, {"error": "not found"})

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    app.port = httpd.server_address[1]
    httpd.daemon_threads = True
    return httpd
