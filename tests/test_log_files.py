"""Each service's own log file (cerberus.log): rotated, size-bounded, not world-readable, and
never the journal (Core #44). Made-up values only."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from cerberus import log as log_mod

UNIT_DIR = Path(__file__).resolve().parents[1] / "systemd"


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield root
    for h in root.handlers:
        if h not in before:
            h.close()
    root.handlers[:] = before
    root.setLevel(level)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_file_is_0640_and_rotates_within_its_bound(tmp_path):
    h = log_mod.file_handler(tmp_path, "cerberus-test", max_bytes=2000, backups=2)
    lg = logging.getLogger("cerberus.test.rotation")
    lg.propagate, lg.level = False, logging.INFO
    lg.addHandler(h)
    try:
        for i in range(500):
            lg.info("line %d with some padding to fill the file", i)
    finally:
        lg.removeHandler(h)
        h.close()
    files = sorted(tmp_path.iterdir())
    assert [f.name for f in files] == ["cerberus-test.log", "cerberus-test.log.1", "cerberus-test.log.2"]
    for f in files:
        assert _mode(f) == 0o640, f.name             # the first file and every one after a rollover
        assert f.stat().st_size <= 2000 + 200
    assert "line 499" in (tmp_path / "cerberus-test.log").read_text()


def test_setup_logging_writes_the_file_only_with_a_log_dir(tmp_path, monkeypatch, root_logger):
    monkeypatch.delenv(log_mod.LOG_DIR_ENV, raising=False)
    log_mod.setup_logging("INFO", service="cerberus-ui")
    assert not any(isinstance(h, log_mod.PrivateRotatingFileHandler) for h in root_logger.handlers)

    monkeypatch.setenv(log_mod.LOG_DIR_ENV, str(tmp_path))
    log_mod.setup_logging("INFO", service="cerberus-ui")
    root_logger.setLevel(logging.INFO)            # basicConfig is a no-op under pytest's own handlers
    logging.getLogger("cerberus.ui").info("hello from a test")
    for h in root_logger.handlers:
        h.flush()
    p = log_mod.log_path(tmp_path, "cerberus-ui")
    assert "hello from a test" in p.read_text() and _mode(p) == 0o640
    fh = [h for h in root_logger.handlers if isinstance(h, log_mod.PrivateRotatingFileHandler)]
    assert fh and fh[0].maxBytes == log_mod.MAX_BYTES and fh[0].backupCount == log_mod.BACKUPS


def test_a_missing_log_folder_costs_only_the_file(tmp_path, monkeypatch, root_logger):
    monkeypatch.setenv(log_mod.LOG_DIR_ENV, str(tmp_path / "nope"))
    log_mod.setup_logging("INFO", service="cerberus")          # no exception
    assert not any(isinstance(h, log_mod.PrivateRotatingFileHandler) for h in root_logger.handlers)


@pytest.mark.parametrize("unit", ["cerberus.service", "cerberus-ui.service", "cerberus-ble.service",
                                  "cerberus-wifi.service"])
def test_units_set_the_log_dir_and_the_ui_has_no_journal(unit):
    text = (UNIT_DIR / unit).read_text()
    assert "Environment=CERBERUS_LOG_DIR=/var/log/cerberus" in text
    assert "systemd-journal" not in text and "adm" not in text.split()
    if "User=cerberus" in text:
        assert "LogsDirectory=cerberus" in text and "LogsDirectoryMode=0750" in text


def test_every_service_name_has_a_unit():
    for svc in log_mod.SERVICES:
        assert (UNIT_DIR / f"{svc}.service").is_file(), svc
