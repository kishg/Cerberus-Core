"""Govee H5179 advertisement decoding.

Exists because the van had no ambient temperature at all and the question that
needed it — does load fall through autumn as fast as solar does? — could only
be argued from physics. Daily DC load correlated with *pack* temperature at
r = +0.016 across 34 days, so the packs were never going to answer it.

Payloads below are real captures from the sensor in the van, 2026-09-24.
"""

from __future__ import annotations

import pytest

from cerberus.govee import GOVEE_COMPANY_ID, decode


def _mfg(hexstr, company=GOVEE_COMPANY_ID):
    return {company: bytes.fromhex(hexstr)}


def test_decodes_a_real_capture():
    r = decode(_mfg("ec0001013408cc1064"))
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct) == (69.8, 43.0, 100)


def test_second_real_capture_resolves_a_tenth_of_a_degree():
    """The two captures differed only in the temperature field: 0x0834 vs
    0x083e, 21.00 C vs 21.10 C. If the byte order were wrong these would not
    land a tenth apart."""
    a = decode(_mfg("ec0001013408cc1064")).air_temp_f
    b = decode(_mfg("ec0001013e08cc1064")).air_temp_f
    assert (a, b) == (69.8, 70.0)


def test_temperature_is_signed():
    """It will read below freezing in winter, which is exactly when the
    cold-soak charge lockout question matters."""
    # -5.00 C = 0xFE0C little-endian
    r = decode(_mfg("ec0001010cfecc1064"))
    assert r.air_temp_f == pytest.approx(23.0, abs=0.1)


@pytest.mark.parametrize("payload,why", [
    ({}, "no manufacturer data at all"),
    (_mfg("ec0001013408cc1064", company=0x004C), "Apple, not Govee"),
    (_mfg("ec000101"), "truncated"),
    (_mfg("ec0001013408cc106400"), "too long"),
    (_mfg("aa0001013408cc1064"), "wrong preamble"),
])
def test_foreign_or_malformed_advertisements_return_none(payload, why):
    """This runs in a scanner callback shared with the pack connections. Any
    passing device's advertisement reaches it, and none of them may raise."""
    assert decode(payload) is None, why


@pytest.mark.parametrize("hexstr", [
    "ec000101b8dfcc1064",   # -82 C
    "ec000101e02ecc1064",   # +120 C
    "ec0001013408a08c1064"[:18],  # humidity 36000 -> 360 %
])
def test_out_of_range_readings_are_dropped(hexstr):
    """A corrupted advertisement must not be stored. A bogus -300 C would
    silently poison every correlation built on this table."""
    r = decode(_mfg(hexstr))
    if r is not None:
        assert -40 <= (r.air_temp_f - 32) * 5 / 9 <= 80
        assert 0 <= r.humidity_pct <= 100


def test_fahrenheit_not_celsius():
    """Ambient in F, equipment in C. The table column is air_temp_f and it must
    actually hold F, or a join against battery_samples.temperature mixes units."""
    r = decode(_mfg("ec0001013408cc1064"))
    assert r.air_temp_f == 69.8, "21.00 C is 69.8 F"
    assert r.air_temp_f != 21.0
