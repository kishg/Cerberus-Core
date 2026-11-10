"""Imported VictronConnect settings files (cerberus.vcsf, cerberus.victron_imports).

The format was decoded from this van's MultiPlus exports (2026-11-10). The
files here are built the same way, with the values of the 2026-11-01 restore:
a reset MultiPlus (absorption 12 V, shut-down 9.3 V) and the restored one.
"""

from __future__ import annotations

import base64
import json
import shutil
import struct
import threading
import urllib.request
import zlib
from pathlib import Path

import pytest

from cerberus import vcsf, victron_imports as vi
from cerberus.db import Db
from cerberus.device_settings import SettingsRecorder
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
S = "/Devices/0/Settings/"

RESET = {S + "AbsorptionVoltage": 12, S + "FloatVoltage": 12,
         S + "InverterDcShutdownVoltage": 9.300000190734863,
         S + "InverterDcRestartVoltage": 10.899999618530273,
         S + "LowDcAlarmLevel": 10.899999618530273, S + "LithiumBattery": 0,
         S + "PowerAssistEnabled": 1, S + "ReadProgress": 100}
RESTORED = {**RESET, S + "AbsorptionVoltage": 14.399999618530273,
            S + "FloatVoltage": 13.399999618530273,
            S + "InverterDcShutdownVoltage": 12.09999942779541,
            S + "InverterDcRestartVoltage": 12.799999237060547,
            S + "LowDcAlarmLevel": 12.300000190734863, S + "LithiumBattery": 1}


def make_vcsf(settings: dict | None = None, serial="HQ2243F7AM3", **meta) -> bytes:
    data = {"/Mgmt/Connection": "VE.Bus", "/VebusChargeState": 2,
            "/Devices/0/SerialNumber": serial, "/FirmwareVersion": 1384,
            "/Ac/Out/L1/V": 120.1, "/Dc/0/Voltage": 13.28, "/Update/FirmwareFiles": None,
            **(RESTORED if settings is None else settings)}
    doc = {"appName": "VictronConnect", "appversion": "v6.33", "productid": "0x2705",
           "minfwversion": "0x568", "structureversion": 1, "description": "", **meta,
           "data": data}
    body = json.dumps(doc).encode()
    return vcsf.MAGIC + struct.pack(">I", len(body)) + zlib.compress(body)


# ---- the format ---------------------------------------------------------------

def test_decodes_and_keeps_only_settings():
    f = vcsf.decode(make_vcsf())
    assert (f.kind, f.serial, f.product_id, f.firmware) == ("vebus", "HQ2243F7AM3", "0x2705", "568")
    s = f.settings()
    assert S + "AbsorptionVoltage" in s and "/FirmwareVersion" in s
    # Live readings, update bookkeeping and progress counters are not settings.
    assert not {"/Ac/Out/L1/V", "/Dc/0/Voltage", "/Update/FirmwareFiles",
                S + "ReadProgress"} & set(s)


@pytest.mark.parametrize("raw", [
    b"", b"PK\x03\x04zipfile", vcsf.MAGIC + struct.pack(">I", 10) + b"not zlib",
    vcsf.MAGIC + struct.pack(">I", 999) + zlib.compress(b'{"data": {}}'),   # wrong length
    vcsf.MAGIC + struct.pack(">I", 2) + zlib.compress(b"[]"),
    vcsf.MAGIC + struct.pack(">I", 2 ** 31) + zlib.compress(b"{}"),
])
def test_rejects_what_isnt_a_vcsf(raw):
    with pytest.raises(vcsf.VcsfError):
        vcsf.decode(raw)


def test_the_restore_diff_reads_like_the_owner_would_say_it():
    changes = {c["name"]: (c["old_text"], c["new_text"]) for c in
               vcsf.diff(vcsf.decode(make_vcsf(RESET)).settings(),
                         vcsf.decode(make_vcsf()).settings())}
    assert changes == {
        "Absorption voltage": ("12.00 V", "14.40 V"),
        "Float voltage": ("12.00 V", "13.40 V"),
        "DC input low shut-down": ("9.30 V", "12.10 V"),
        "DC input low restart": ("10.90 V", "12.80 V"),
        "DC input low pre-alarm": ("10.90 V", "12.30 V"),
        "Lithium battery": ("off", "on"),
    }


def test_formatting():
    assert vcsf.fmt("/FirmwareVersion", 1384) == "v568"
    assert vcsf.fmt(S + "MaximumAbsorptionTime", 60) == "60 min"
    assert vcsf.fmt(S + "StopChargingBelowTemperature", -20.5) == "-20.5 °C"
    assert vcsf.fmt(S + "ChargeCharacteristic", 3) == "3"        # no guessed names
    assert vcsf.fmt(S + "Whatever", None) == "not set"
    assert vcsf.label(S + "VsOnWhenUdcLowerThen")[0] == "Vs on when udc lower then"
    assert vcsf.label("/Ac/In/1/CurrentLimit")[0] == "AC input 1 current limit (now)"


# ---- storage and the report -----------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("x: 1\n")
    return p


def test_import_keeps_the_file_and_reports_changes(cfg):
    vi.add(cfg, "multi-reset.vcsf", make_vcsf(RESET), now=100)
    e = vi.add(cfg, "multi restored!.vcsf", make_vcsf(), now=200)
    assert e["name"] == "multi restored_.vcsf" and e["device"] == "HQ2243F7AM3"
    d = vi.dir_for(cfg)
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (d / e["file"]).read_bytes() == make_vcsf()           # the original, as a backup
    (dev,) = vi.view(cfg)["devices"]
    newest, first = dev["imports"]
    assert newest["id"] == e["id"] and len(newest["changes"]) == 6 and first["changes"] is None
    assert any(s["name"] == "Absorption voltage" and s["text"] == "14.40 V" for s in dev["settings"])


def test_devices_are_kept_apart(cfg):
    vi.add(cfg, "a.vcsf", make_vcsf(RESET), now=100)
    vi.add(cfg, "b.vcsf", make_vcsf(serial="HQOTHER"), now=200)
    v = vi.view(cfg)["devices"]
    assert [d["device"] for d in v] == ["HQOTHER", "HQ2243F7AM3"]
    assert all(d["imports"][0]["changes"] is None for d in v)


def test_bad_uploads_are_refused(cfg):
    for raw in (b"", b"hello", b"x" * (vi.MAX_FILE + 1)):
        with pytest.raises(vi.VictronImportError):
            vi.add(cfg, "x.vcsf", raw)
    assert vi.read(cfg) == []


def test_delete(cfg):
    e = vi.add(cfg, "a.vcsf", make_vcsf(), now=100)
    assert vi.delete(cfg, e["id"]) and not vi.delete(cfg, e["id"])
    assert vi.read(cfg) == [] and not (vi.dir_for(cfg) / e["file"]).exists()


def test_a_damaged_file_shows_as_unreadable(cfg):
    e = vi.add(cfg, "a.vcsf", make_vcsf(), now=100)
    (vi.dir_for(cfg) / e["file"]).write_bytes(b"garbage")
    (dev,) = vi.view(cfg)["devices"]
    assert dev["imports"][0]["readable"] is False and dev["settings"] == []


def test_checked_against_what_the_cerbo_reports(cfg, tmp_path):
    db = Db(str(tmp_path / "t.db"))
    rec = SettingsRecorder(db)
    rec.on_message("N/x/vebus/276/Devices/0/Settings/PowerAssistEnabled", 0, now=50)
    rec.on_message("N/x/vebus/276/FirmwareVersion", 1384, now=50)
    rec.on_message("N/x/vebus/276/Devices/0/Settings/WeakAcInput", False, now=50)
    vi.add(cfg, "a.vcsf", make_vcsf({**RESTORED, S + "WeakAcInput": 0}), now=100)
    (dev,) = vi.view(cfg, db.conn)["devices"]
    check = {c["name"]: c for c in dev["live_check"]}
    assert set(check) == {"PowerAssist", "Firmware", "Weak AC input"}
    # The file's 0 and MQTT's false are the same switch position.
    assert check["Firmware"]["match"] is True and check["Weak AC input"]["match"] is True
    assert check["PowerAssist"]["match"] is False and check["PowerAssist"]["live_text"] == "off"


# ---- through the API -------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    db = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body, token=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1" + path, body=json.dumps(body),
              headers={"Content-Type": "application/json",
                       **({"Authorization": f"Bearer {token}"} if token else {})})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_import_through_the_api(server):
    cfg, port = server
    body = {"name": "multi.vcsf", "data": base64.b64encode(make_vcsf()).decode()}
    assert _post(port, "/victron/import", body)[0] == 401
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    code, r = _post(port, "/victron/import", body, tok)
    assert code == 200 and r["import"]["device"] == "HQ2243F7AM3"
    assert _post(port, "/victron/import", {"name": "x", "data": "!!!"}, tok)[0] == 400
    code, r2 = _post(port, "/victron/import",
                     {"name": "x.rvsc", "data": base64.b64encode(b"VEConfig setting").decode()}, tok)
    assert code == 400 and "not a VictronConnect settings file" in r2["error"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/victron/imports", timeout=5) as resp:
        v = json.loads(resp.read())
    assert v["devices"][0]["imports"][0]["name"] == "multi.vcsf"
    assert _post(port, "/victron/import/delete", {"id": r["import"]["id"]}, tok)[0] == 200
    assert vi.read(cfg) == []
