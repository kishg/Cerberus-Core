"""Run the Cerberus UI.

    python -m cerberus.ui --config config.yaml [--db PATH] [--host H] [--port N]
    python -m cerberus.ui --config config.yaml --set-pin     # on the box

Needs no MQTT password: it reads only config.yaml's analysis and ui sections
(config.load_ui) and the database, read-only.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal

from .. import config as cfg_mod
from ..log import setup_logging
from . import auth
from .server import App, serve

log = logging.getLogger("cerberus.ui")


def _stop(signum, frame) -> None:
    # SIGTERM from launchd/systemd: unwind serve_forever like Ctrl-C does.
    raise KeyboardInterrupt


def main() -> int:
    ap = argparse.ArgumentParser(description="Cerberus UI (read-only)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", help="database path; default CERBERUS_SQLITE_PATH, "
                                 "else storage.sqlite_path from --config")
    ap.add_argument("--host", help="override ui.host")
    ap.add_argument("--port", type=int, help="override ui.port")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--set-pin", action="store_true",
                    help="set or change the settings PIN, then exit")
    args = ap.parse_args()
    setup_logging(args.log_level)

    if args.set_pin:
        pin = getpass.getpass("New settings PIN (4-12 digits): ")
        if getpass.getpass("Again: ") != pin:
            print("PINs differ; nothing changed.")
            return 1
        try:
            auth.set_pin(args.config, pin)
        except auth.AuthError as err:
            print(err)
            return 1
        print(f"PIN set ({auth.pin_path(args.config)}). Everyone signed in has been signed out.")
        return 0

    ui = cfg_mod.load_ui(args.config)
    db = args.db or os.environ.get("CERBERUS_SQLITE_PATH") or ui.sqlite_path
    host = args.host or ui.host
    port = args.port or ui.port
    app = App.from_config(args.config, db)
    httpd = serve(app, host, port)
    signal.signal(signal.SIGTERM, _stop)
    log.info("Cerberus UI on http://%s:%d (db %s, read-only)", host, port, db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log.info("Cerberus UI stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
