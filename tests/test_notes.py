"""Owner notes on findings (cerberus.notes): stored beside config.yaml, shown
on their finding, and used to say what changed after them."""

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

from cerberus import findings as F
from cerberus import notes
from cerberus.db import Db
from cerberus.ui import api, auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
NY = ZoneInfo("America/New_York")
NOW = int(datetime(2026, 10, 31, 15, 0, tzinfo=NY).timestamp())
DAY = 86400
S = F.Settings(tz=NY)


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    return p


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "f.db"))


def _solar_day(db, day, v):
    db.conn.execute("INSERT INTO solar_days (day, metrics, flags, created_ts) VALUES (?, ?, '[]', 0)",
                    (day, json.dumps({"path_mohm_at_absorption": v})))


def _event(db, ts, typ, **detail):
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, ?, 'warn', ?)",
                    (ts, typ, json.dumps({"detail": detail})))


def _ts(day, hour=18):
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=NY).timestamp())


# --- the file ------------------------------------------------------------------------

def test_add_read_delete(cfg):
    a = notes.add(cfg, "solar_path_resistance", "  Tightened MPPT lugs  ", "Solar…", now=100)
    b = notes.add(cfg, "early_float", "Checked absorption", now=50)
    assert [n["id"] for n in notes.read(cfg)] == [b["id"], a["id"]]      # oldest first
    assert notes.read(cfg)[1]["text"] == "Tightened MPPT lugs"
    assert notes.path_for(cfg).stat().st_mode & 0o777 == 0o600
    assert notes.delete(cfg, a["id"]) and not notes.delete(cfg, a["id"])
    assert [n["key"] for n in notes.read(cfg)] == ["early_float"]


@pytest.mark.parametrize("key, text", [("", "x"), ("k", "   "), ("k", "x" * 1001), ("k" * 81, "x")])
def test_bad_notes_are_refused(cfg, key, text):
    with pytest.raises(notes.NoteError):
        notes.add(cfg, key, text)
    assert notes.read(cfg) == []


def test_a_damaged_file_is_no_notes(cfg):
    notes.path_for(cfg).write_text("{not json")
    assert notes.read(cfg) == []
    notes.path_for(cfg).write_text(json.dumps({"notes": [{"id": "x"}, {"id": "y", "key": "k", "ts": 1, "text": "t"}]}))
    assert [n["id"] for n in notes.read(cfg)] == ["y"]


# --- as context for findings ---------------------------------------------------------

def _path_finding(db, ns):
    for day, v in (("2026-10-25", 90), ("2026-10-26", 92), ("2026-10-28", 80), ("2026-10-30", 78)):
        _solar_day(db, day, v)
    return {f.key: f for f in F.derive(db.conn, S, NOW, ns)}["solar_path_resistance"]


def test_a_series_finding_compares_before_and_after_the_note(db):
    n = {"id": "1", "key": "solar_path_resistance", "ts": _ts("2026-10-27"), "text": "lugs", "title": ""}
    f = _path_finding(db, [n])
    assert f.notes == [n]
    assert ("Since your note on Oct 27: solar path resistance averaged 79 mΩ over 2 readings, "
            "against 91 mΩ before.") in f.explain


def test_no_comparison_until_there_is_a_reading_after_the_note(db):
    n = {"id": "1", "key": "solar_path_resistance", "ts": _ts("2026-10-31"), "text": "lugs", "title": ""}
    f = _path_finding(db, [n])
    assert f.notes == [n] and "Since your note" not in f.explain


def test_an_event_finding_says_whether_it_came_back(db):
    _event(db, NOW - 5 * DAY, "solar_output_open", pv_voltage=40)
    _event(db, NOW - 5 * DAY + 600, "solar_output_open_cleared")
    note = {"id": "1", "key": "solar_output_open", "ts": NOW - 4 * DAY, "text": "new breaker", "title": ""}
    f = {f.key: f for f in F.derive(db.conn, S, NOW, [note])}["solar_output_open"]
    assert "It has not come back since your note on Oct 27." in f.explain
    _event(db, NOW - DAY, "solar_output_open", pv_voltage=40)
    f = {f.key: f for f in F.derive(db.conn, S, NOW, [note])}["solar_output_open"]
    assert "It has come up again since your note on Oct 27." in f.explain


def test_a_state_finding_never_claims_it_came_back(db):
    note = {"id": "1", "key": "no_full_charge", "ts": NOW - DAY, "text": "plugged in", "title": ""}
    f = {f.key: f for f in F.derive(db.conn, S, NOW, [note])}["no_full_charge"]
    assert f.notes == [note] and "since your note" not in f.explain


def test_findings_view_keeps_notes_whose_finding_is_gone(db):
    ns = [{"id": "1", "key": "no_full_charge", "ts": 1, "text": "a", "title": "No full charge"},
          {"id": "2", "key": "joint_heating", "ts": 2, "text": "b", "title": "Hot joint"}]
    v = api.findings_view(db.conn, api.Context(tz=NY), NOW, ns)
    assert [n["id"] for n in v["other_notes"]] == ["2"]
    assert next(f for f in v["findings"] if f["key"] == "no_full_charge")["notes"][0]["id"] == "1"


# --- through the API -----------------------------------------------------------------

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
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/v1" + path, body=json.dumps(body),
              headers={"Content-Type": "application/json",
                       **({"Authorization": f"Bearer {token}"} if token else {})})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_notes_through_the_api(server):
    cfg, port = server
    body = {"key": "no_full_charge", "title": "No full charge in a long time", "text": "On shore power"}
    assert _post(port, "/notes", body)[0] == 401 and notes.read(cfg) == []
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    code, r = _post(port, "/notes", body, tok)
    assert code == 200 and r["note"]["text"] == "On shore power"
    assert _post(port, "/notes", {**body, "text": ""}, tok)[0] == 400
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/findings", timeout=5) as resp:
        v = json.loads(resp.read())
    f = next(f for f in v["findings"] if f["key"] == "no_full_charge")
    assert [n["text"] for n in f["notes"]] == ["On shore power"]
    assert _post(port, "/notes/delete", {"id": r["note"]["id"]})[0] == 401
    assert _post(port, "/notes/delete", {"id": r["note"]["id"]}, tok)[0] == 200
    assert _post(port, "/notes/delete", {"id": r["note"]["id"]}, tok)[0] == 404
    assert notes.read(cfg) == []


# --- known ---------------------------------------------------------------------------

def _f(db, ns, key):
    return {f.key: f for f in F.derive(db.conn, S, NOW, ns)}[key]


def test_known_event_finding_until_it_happens_again(db):
    _event(db, NOW - 5 * DAY, "alarm:vebus/276/LowBattery")
    note = {"id": "k", "key": "inverter_low_battery", "ts": NOW - 4 * DAY, "text": "heater runs",
            "title": "", "known": True}
    f = _f(db, [note], "inverter_low_battery")
    assert f.status == "open" and f.known == {"id": "k", "ts": note["ts"]}
    _event(db, NOW - DAY, "alarm:vebus/276/LowBattery")
    f = _f(db, [note], "inverter_low_battery")
    assert f.known is None and "it has happened again since, so it is flagged again" in f.explain


def test_known_measured_finding_holds_through_its_run_but_not_a_new_one(db):
    note = {"id": "k", "key": "solar_path_resistance", "ts": _ts("2026-10-27"), "text": "known",
            "title": "", "known": True}
    assert _path_finding(db, [note]).known is not None          # same bad run as the note
    _solar_day(db, "2026-10-29", 60)                             # cleared, then above again
    assert _f(db, [note], "solar_path_resistance").known is None


def test_a_plain_note_does_not_mark_known_and_known_sorts_last(db):
    _event(db, NOW - 5 * DAY, "alarm:vebus/276/LowBattery")
    plain = {"id": "p", "key": "inverter_low_battery", "ts": NOW - DAY, "text": "x", "title": ""}
    assert _f(db, [plain], "inverter_low_battery").known is None
    known = {**plain, "id": "k", "known": True}
    found = F.derive(db.conn, S, NOW, [known])
    assert [f.key for f in found] == ["no_full_charge", "inverter_low_battery"]
    v = api.now_view(db.conn, api.Context(tz=NY), NOW, [known])
    assert [f["key"] for f in v["findings"]] == ["no_full_charge"] and v["known_count"] == 1


def test_set_known_and_through_the_api(server):
    cfg, port = server
    auth.set_pin(cfg, "2468")
    tok = _post(port, "/auth/login", {"pin": "2468"})[1]["token"]
    body = {"key": "no_full_charge", "title": "No full charge", "text": "In storage", "known": True}
    n = _post(port, "/notes", body, tok)[1]["note"]
    assert n["known"] is True and notes.read(cfg)[0]["known"] is True
    assert _post(port, "/notes/known", {"id": n["id"], "known": False})[0] == 401
    assert _post(port, "/notes/known", {"id": n["id"], "known": False}, tok)[0] == 200
    assert notes.read(cfg)[0] == {**n, "known": False}
    assert _post(port, "/notes/known", {"id": "nope", "known": True}, tok)[0] == 404
