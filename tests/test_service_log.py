"""The service log (cerberus.service_log): the owner's service items, beside
config.yaml, and the chassis events worked out from data already kept."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cerberus import service_log as S
from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
NY = ZoneInfo("America/New_York")
NOW = int(datetime(2026, 11, 20, 15, 0, tzinfo=NY).timestamp())
DAY = 86400
OIL = {"date": "2026-11-02", "category": "oil", "text": "Oil and filter, 5W-30",
       "shop": "Sprinter dealer", "odometer_mi": "48210", "cost": 189.5}


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    return p


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "s.db"))


# --- the file ------------------------------------------------------------------------

def test_add_update_delete(cfg):
    a = S.add(cfg, OIL, now=NOW)
    b = S.add(cfg, {"date": "2026-10-01", "category": "tyres", "text": "Rotated"}, now=NOW)
    assert a["odometer_mi"] == 48210.0 and a["cost"] == 189.5 and a["shop"] == "Sprinter dealer"
    assert [e["id"] for e in S.read(cfg)] == [b["id"], a["id"]]          # by day
    assert S.update(cfg, a["id"], {**OIL, "odometer_mi": 48201}, now=NOW)["odometer_mi"] == 48201
    assert S.update(cfg, "nope", OIL, now=NOW) is None
    assert S.delete(cfg, b["id"]) and not S.delete(cfg, b["id"])
    assert [e["odometer_mi"] for e in S.read(cfg)] == [48201]
    assert S.path_for(cfg).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("change", [
    {"date": ""}, {"date": "11/02/2026"}, {"date": "2026-12-25"}, {"category": "paint"},
    {"text": "  "}, {"text": "x" * 1001}, {"odometer_mi": "lots"}, {"odometer_mi": -5},
    {"cost": 2e6}, {"shop": "s" * 121},
])
def test_bad_entries_are_refused(cfg, change):
    with pytest.raises(S.ServiceError):
        S.add(cfg, {**OIL, **change}, now=NOW)
    assert S.read(cfg) == []


def test_optional_fields_can_be_blank(cfg):
    e = S.add(cfg, {**OIL, "odometer_mi": "", "cost": None, "shop": ""}, now=NOW)
    assert e["odometer_mi"] is None and e["cost"] is None and e["shop"] == ""


def test_a_damaged_file_is_no_entries(cfg):
    S.path_for(cfg).write_text("{not json")
    assert S.read(cfg) == []
    S.path_for(cfg).write_text(json.dumps({"entries": [
        {"id": "x"}, {"id": "y", "date": "2026-1-1", "category": "oil", "text": "t"},
        {"id": "z", "date": "2026-11-01", "category": "?", "text": "t"}]}))
    assert [(e["id"], e["category"]) for e in S.read(cfg)] == [("z", "other")]


# --- chassis events ------------------------------------------------------------------

def test_trouble_codes_set_pending_and_cleared(db):
    db.insert_dtcs(NOW - 5 * DAY, "03", [], "4300")
    db.insert_dtcs(NOW - 5 * DAY, "07", ["P0401"], "raw")
    db.insert_dtcs(NOW - 4 * DAY, "03", ["P0401", "P2002"], "raw")   # the pending one confirmed
    db.insert_dtcs(NOW - 4 * DAY, "07", ["P0401"], "raw")
    db.insert_dtcs(NOW - 3 * DAY, "03", None, None)                  # a failed read: no evidence
    db.insert_dtcs(NOW - 2 * DAY, "03", ["P2002"], "raw")
    db.insert_dtcs(NOW - DAY, "03", [], "4300")
    got = [(e["ts"], e["kind"], e["code"]) for e in S.dtc_events(db.conn)]
    assert got == [(NOW - 5 * DAY, "dtc_pending", "P0401"),
                   (NOW - 4 * DAY, "dtc_set", "P0401"), (NOW - 4 * DAY, "dtc_set", "P2002"),
                   (NOW - 2 * DAY, "dtc_cleared", "P0401"),
                   (NOW - DAY, "dtc_cleared", "P2002")]


def test_codes_already_stored_at_the_first_read_are_set_then(db):
    db.insert_dtcs(NOW - DAY, "03", ["P0299"], "raw")
    assert [e["kind"] for e in S.dtc_events(db.conn)] == ["dtc_set"]


def test_engine_light_on_and_off(db):
    db.insert_obd_samples(NOW - 3 * DAY, [("7E8", "mil_on", "x", 0.0)])
    db.insert_obd_samples(NOW - 2 * DAY, [("7E8", "mil_on", "x", 1.0)])
    db.insert_vehicle_samples(NOW - 2 * DAY + 60, "mb", {"engine_light": 1.0})
    db.insert_obd_samples(NOW - DAY, [("7E8", "mil_on", "x", 0.0)])
    assert [(e["ts"], e["kind"]) for e in S.mil_events(db.conn)] == [
        (NOW - 2 * DAY, "mil_on"), (NOW - DAY, "mil_off")]


def test_service_countdown_reset(db):
    for i, v in enumerate((900, 450, 120, 10000, 9950)):
        db.insert_vehicle_samples(NOW - (5 - i) * DAY, "mb", {"service_due_mi": float(v)})
    ev = S.reset_events(db.conn)
    assert [(e["ts"], e["next_mi"]) for e in ev] == [(NOW - 2 * DAY, 10000)]
    assert ev[0]["detail"] == "Next service in 10,000 mi (was 120)."


def test_odometer_is_the_nearest_reading_if_close(db):
    db.insert_vehicle_samples(NOW - 10 * DAY, "mb", {"odometer_mi": 47000.0})
    db.insert_obd_samples(NOW - DAY, [("7E8", "odometer_mi", "x", 48100.4)])
    db.insert_vehicle_samples(NOW - 2 * 3600, "mb", {"odometer_mi": 48150.0})
    assert S.odometer_at(db.conn, NOW) == 48150
    assert S.odometer_at(db.conn, NOW - DAY + 60) == 48100
    assert S.odometer_at(db.conn, NOW - 6 * DAY) is None                # nothing near


def test_view_merges_newest_first(db, cfg):
    db.insert_dtcs(NOW - DAY, "03", ["P2002"], "raw")
    db.insert_obd_samples(NOW - DAY, [("7E8", "odometer_mi", "x", 48300.0)])
    S.add(cfg, OIL, now=NOW)
    v = S.view(db.conn, S.read(cfg), NY)
    assert [i["kind"] for i in v["items"]] == ["dtc_set", "service"]
    assert v["items"][0]["odometer_mi"] == 48300
    svc = v["items"][1]
    assert svc["title"] == "Oil and filter" and svc["odometer_mi"] == 48210
    assert svc["ts"] == int(datetime(2026, 11, 2, 12, tzinfo=NY).timestamp())
    assert v["categories"]["def"] == "DEF (AdBlue)"


# --- through the API -----------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfg)
    db = Db(str(tmp_path / "s.db"))
    db.insert_dtcs(NOW - DAY, "03", ["P2002"], "raw")
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body, token=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1" + path, body=json.dumps(body),
              headers={"Content-Type": "application/json",
                       **({"Authorization": f"Bearer {token}"} if token else {})})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1{path}", timeout=5) as r:
        return json.loads(r.read())


def test_service_log_through_the_api(server):
    cfg, port = server
    assert _post(port, "/service", OIL)[0] == 401 and S.read(cfg) == []
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    code, r = _post(port, "/service", OIL, tok)
    assert code == 200 and r["entry"]["category"] == "oil"
    assert _post(port, "/service", {**OIL, "text": ""}, tok)[0] == 400
    eid = r["entry"]["id"]
    assert _post(port, "/service/update", {**OIL, "id": eid, "text": "Oil, filter, cabin filter"},
                 tok)[0] == 200
    kinds = [(i["kind"], i.get("text")) for i in _get(port, "/chassis")["service_log"]["items"]]
    assert kinds == [("dtc_set", None), ("service", "Oil, filter, cabin filter")]
    assert [i["kind"] for i in _get(port, "/service")["items"]] == ["dtc_set", "service"]
    assert _post(port, "/service/delete", {"id": eid})[0] == 401
    assert _post(port, "/service/delete", {"id": eid}, tok)[0] == 200
    assert _post(port, "/service/delete", {"id": eid}, tok)[0] == 404
    assert _post(port, "/service/update", {**OIL, "id": eid}, tok)[0] == 404
