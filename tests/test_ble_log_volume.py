"""Out-of-range connect failures must not fill the disk.

Measured 2026-10-01 after 2.6 days parked at home with the laptop in the house
and the packs in the van: the sidecar log had reached 5.2 MB and was growing at
**5,773 lines/day** across three packs — on track for roughly 250 MB a year.
The plist comment had estimated ~4,300/day, so it was worse than assumed.

Every one of those lines said the same thing. Being out of BLE range is correct
and expected behaviour (see the RANGE note in
launchd/com.cerberus.ble.plist), it is fully described by its first line, and it
resolves by itself on return to the van. Log rotation would have managed the
symptom; this collapses the source.

What must survive the fix: the log still has to show that the condition is
ongoing, or a genuinely stuck sidecar becomes invisible. Hence the hourly
summary, and hence the lower bound in test_a_day_out_of_range_is_quiet.
"""

from __future__ import annotations

import logging

from cerberus.ble import _ConnectFailureLog

LOGGER = "cerberus.ble"
ERR = OSError("Device with address 4D6F0BCD was not found")


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _at_least_info(caplog):
    """What the sidecar actually writes: launchd runs it at --log-level INFO."""
    return [r for r in caplog.records if r.levelno >= logging.INFO]


def test_first_failure_is_loud_and_the_rest_are_not(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = _ConnectFailureLog("pack 1")
    f.failure(ERR, now=0.0)
    assert len(_warnings(caplog)) == 1
    assert "not found" in _warnings(caplog)[0].getMessage()

    for i in range(1, 20):
        f.failure(ERR, now=i * 90.0)
    assert len(_warnings(caplog)) == 1, "repeat failures must not add WARNINGs"


def test_hourly_summary_proves_the_condition_is_still_live(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = _ConnectFailureLog("pack 1")
    f.failure(ERR, now=0.0)
    f.failure(ERR, now=3599.0)
    assert len(_warnings(caplog)) == 1, "too early for a summary"
    f.failure(ERR, now=3600.0)
    w = _warnings(caplog)
    assert len(w) == 2
    msg = w[1].getMessage()
    assert "still unreachable" in msg and "3 consecutive" in msg


def test_recovery_reports_how_long_it_took_and_resets(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = _ConnectFailureLog("pack 2")
    for i in range(5):
        f.failure(ERR, now=i * 90.0)
    caplog.clear()
    f.success()
    assert "connected after 5 failed attempt(s)" in caplog.records[0].getMessage()

    # Counter reset: the next failure is a fresh sequence and loud again.
    caplog.clear()
    f.failure(ERR, now=10_000.0)
    assert len(_warnings(caplog)) == 1


def test_connecting_line_is_info_once_then_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = _ConnectFailureLog("pack 3")
    f.connecting("ADDR")
    assert _at_least_info(caplog)[0].getMessage().endswith("ADDR")
    f.failure(ERR, now=0.0)
    caplog.clear()
    f.connecting("ADDR")
    assert _at_least_info(caplog) == [], "retry attempts belong at DEBUG"


def test_a_day_out_of_range_is_quiet(caplog):
    """The regression, in the units the problem was measured in.

    One pack, 24 h out of range at the observed ~90 s cycle: 960 attempts. At
    --log-level INFO that used to be 1,920 lines (a connecting + an error for
    each). It must now be a couple of dozen — but NOT zero, or a genuinely
    wedged sidecar would look identical to a healthy one.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    f = _ConnectFailureLog("pack 1")
    cycles = 960
    for i in range(cycles):
        t = i * 90.0
        f.connecting("ADDR")
        f.failure(ERR, now=t)

    emitted = len(_at_least_info(caplog))
    assert emitted < 30, f"still noisy: {emitted} lines/day"
    assert emitted >= 20, f"too quiet to notice a stuck sidecar: {emitted}"
    # Three packs, for comparison against the measured 5,773 lines/day.
    assert emitted * 3 < 100
