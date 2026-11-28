"""Logging for every Cerberus service: stdout (the journal under systemd, the
launchd log on a Mac) and, on a box, a size-bounded file per service.

The file is what Settings > Support > Download support file reads
(cerberus.supportfile). The UI is never given the system journal, which also
holds logins, sudo commands, other services' peers and Wi-Fi clients: it
reads only Cerberus's own files.

The file is written only when ``CERBERUS_LOG_DIR`` is set (the systemd units
set it to /var/log/cerberus; the folder is 0750 cerberus:cerberus). Each
service writes ``<service>.log`` there, owner read-write and group read
(0640), rotated at MAX_BYTES with BACKUPS older files kept, so four services
take at most 4 x MAX_BYTES x (BACKUPS + 1) of disk. A service running as
root (cerberus-wifi) gives its file the folder's group, so the UI can read it.
A file that can't be opened costs only the file: stdout carries on.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DIR_ENV = "CERBERUS_LOG_DIR"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3
FILE_MODE = 0o640
# The services that write a file, as named in systemd/ (the support file reads these).
SERVICES = ("cerberus", "cerberus-ui", "cerberus-ble", "cerberus-wifi")


def log_path(directory: str | Path, service: str) -> Path:
    return Path(directory) / f"{service}.log"


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler whose files (the first and every one after a rollover) are 0640,
    with the folder's group when the service runs as root."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, FILE_MODE)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                gid = os.stat(os.path.dirname(self.baseFilename)).st_gid
                os.chown(self.baseFilename, -1, gid)
        except OSError:
            pass
        return stream


def file_handler(directory: str | Path, service: str, fmt: str = FORMAT,
                 max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> logging.Handler:
    """The service's file handler. Raises OSError when the folder is missing or not writable."""
    if not Path(directory).is_dir():
        raise FileNotFoundError(f"no log folder {directory}")
    h = PrivateRotatingFileHandler(log_path(directory, service), maxBytes=max_bytes,
                                   backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter(fmt))
    return h


def setup_logging(level: str = "INFO", service: str | None = None, fmt: str = FORMAT) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        stream=sys.stdout,
    )
    directory = os.environ.get(LOG_DIR_ENV)
    if not (service and directory):
        return
    try:
        logging.getLogger().addHandler(file_handler(directory, service, fmt))
    except OSError as err:
        logging.getLogger("cerberus.log").warning("no log file for %s (stdout only): %s", service, err)
