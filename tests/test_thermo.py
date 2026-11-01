"""Bluetooth thermometers of several brands (cerberus.thermo). The H5179
vector is a live capture from this van; the rest are built from each
format's published description, so they pin the decoders to the spec."""

from __future__ import annotations

import struct

import pytest

from cerberus import thermo


def f(c):
    return round(c * 9 / 5 + 32, 1)


def test_govee_h5179_live_capture():
    r = thermo.decode({0x8801: bytes.fromhex("ec000101340 8cc1064".replace(" ", ""))})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(21.0), 43.0, 100, "Govee H5179")


def _packed(t, h):
    raw = int(round(abs(t) * 10)) * 1000 + int(round(h * 10))
    return (raw | (0x800000 if t < 0 else 0)).to_bytes(3, "big")


@pytest.mark.parametrize("t, h", [(21.5, 43.5), (-3.2, 80.1)])
def test_govee_h5075_packed(t, h):
    r = thermo.decode({0xEC88: b"\x00" + _packed(t, h) + bytes([77, 0])})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(t), h, 77, "Govee H5075")


def test_govee_h5074():
    d = b"\x00" + struct.pack("<hH", 2150, 4400) + bytes([90, 2])
    r = thermo.decode({0xEC88: d})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(21.5), 44.0, 90, "Govee H5074")


def test_govee_h51xx():
    r = thermo.decode({0x0001: b"\x01\x01" + _packed(18.2, 55.0) + bytes([60])})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct) == (f(18.2), 55.0, 60)


def test_bthome_v2():
    svc = {thermo.BTHOME: bytes.fromhex("40 00 01 01 5d 02 ca 09 03 bf 13".replace(" ", ""))}
    r = thermo.decode({}, svc)
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(25.06), round(50.55, 1), 93, "BTHome")


def test_bthome_encrypted_is_not_decoded():
    assert thermo.decode({}, {thermo.BTHOME: bytes.fromhex("41 02 ca 09".replace(" ", ""))}) is None


def test_xiaomi_pvvx():
    d = bytes(6) + struct.pack("<hHHBBB", 2150, 4520, 2950, 80, 7, 0)
    r = thermo.decode({}, {thermo.ENV_SENSING: d})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(21.5), 45.2, 80, "Xiaomi (pvvx)")


def test_xiaomi_atc():
    d = bytes(6) + struct.pack(">hBBHB", 215, 45, 80, 2950, 7)
    r = thermo.decode({}, {thermo.ENV_SENSING: d})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(21.5), 45.0, 80, "Xiaomi (ATC)")


@pytest.mark.parametrize("b4, t", [(0x95, 21.5), (0x15, -21.5)])
def test_switchbot_meter(b4, t):
    r = thermo.decode({}, {thermo.SWITCHBOT[0]: bytes([0x54, 0x00, 0x64, 0x05, b4, 0x2D])})
    assert (r.air_temp_f, r.humidity_pct, r.battery_pct, r.model) == (f(t), 45.0, 100, "SwitchBot Meter")


@pytest.mark.parametrize("mfr, svc", [
    ({}, {}), ({76: b"\x02\x15"}, {}),                                  # an iPhone
    ({0xEC88: b"\x00\xff\xff\xff\x64\x00"}, {}),                         # out of range
    ({}, {thermo.ENV_SENSING: b"\x00\x01"}),                             # short
    ({}, {thermo.BTHOME: b"\x40\x02\xca"}),                              # truncated
])
def test_not_a_sensor_or_garbage(mfr, svc):
    assert thermo.decode(mfr, svc) is None
