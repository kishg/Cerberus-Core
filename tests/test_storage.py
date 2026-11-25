"""Storage mode (cerberus.storage, Core #54).

- The state file: start, end, the optional end date and "stays online".
- Findings a stored van trips by design can't open, but an open one isn't cleared.
- Gaps while stored are explained as storage.
- A suggestion when the van looks stored, never switched on silently.
- The collector turns changes into events and ends storage on a drive.
- The UI: read it, change it with the PIN, and phone fixes don't move a stored van.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import threading
import time
import types
from datetime import date
from pathlib import Path

import pytest

from cerberus import findings as F
from cerberus import findings_log, health, storage
from cerberus.__main__ import KV_STORAGE, Cerberus
from cerberus.db import Db
from cerberus.events import Severity, make as make_event
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
NOW = 1_800_000_000
DAY = 86400


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    return p


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "c.db"))
    yield d
    d.close()


# -- the state file -------------------------------------------------------------------

def test_start_and_end(cfg):
    assert storage.read(cfg) == storage.State()
    s = storage.start(cfg, NOW, until="2027-03-01", online=True)
    assert (s.on, s.since, s.until, s.online, s.by) == (True, NOW, "2027-03-01", True, "owner")
    assert stat.S_IMODE(os.stat(storage.path_for(cfg)).st_mode) == 0o600
    again = storage.start(cfg, NOW + 100, until=None)            # changing details keeps "since"
    assert again.since == NOW and again.until is None
    e = storage.end(cfg, NOW + 200, reason="owner")
    assert (e.on, e.since, e.ended_reason) == (False, NOW, "owner")
    assert storage.end(cfg, NOW + 300) == e                       # already off: no change
    with pytest.raises(storage.StorageError):
        storage.start(cfg, NOW, until="next spring")


def test_damaged_file_is_off(cfg):
    storage.path_for(cfg).write_text("{not json")
    assert storage.read(cfg).on is False
    storage.path_for(cfg).write_text('{"on": true, "surprise": 1}')
    assert storage.read(cfg).on is True


def test_due_passed():
    s = storage.State(on=True, since=NOW, until="2027-03-01")
    assert not s.due_passed(date(2027, 3, 1)) and s.due_passed(date(2027, 3, 2))
    assert not storage.State(on=True).due_passed(date(2030, 1, 1))


# -- findings -------------------------------------------------------------------------

def _f(key, status=F.OPEN):
    return F.Finding(key=key, title=key, severity=F.ATTENTION, status=status, explain="",
                     action="", evidence={}, first_seen=NOW - DAY, last_seen=NOW)


def test_quiet_findings_cant_open_but_stay_open(cfg, db):
    stored = storage.start(cfg, NOW)
    found = [_f("no_full_charge"), _f("battery_high_voltage")]
    assert [f.key for f in storage.quiet(found, set(), stored)] == ["battery_high_voltage"]
    assert [f.key for f in storage.quiet(found, {"no_full_charge"}, stored)] == ["no_full_charge",
                                                                                "battery_high_voltage"]
    assert storage.quiet(found, set(), storage.State()) == found

    # Through the log: an open quiet finding is neither cleared nor duplicated while stored.
    findings_log.sync(db.conn, [_f("no_full_charge")], NOW)
    open_keys = {r[0] for r in db.conn.execute("SELECT key FROM finding_log WHERE cleared_ts IS NULL")}
    findings_log.sync(db.conn, storage.quiet([_f("no_full_charge")], open_keys, stored), NOW + 600)
    rows = db.conn.execute("SELECT key, cleared_ts FROM finding_log").fetchall()
    assert rows == [("no_full_charge", None)]


def test_protective_findings_are_never_quiet():
    for key in ("battery_high_voltage", "inverter_low_battery", "starter_battery", "pack_imbalance",
                "joint_heating"):
        assert key not in storage.QUIET_IN_STORAGE


# -- health ---------------------------------------------------------------------------

def test_gaps_while_stored_are_explained_as_storage(db):
    db.insert_event(make_event("storage_started", Severity.INFO, {}))
    db.conn.execute("UPDATE events SET ts = ? WHERE type = 'storage_started'", (NOW - 10 * DAY,))
    assert storage.spans(db.conn, NOW) == [(NOW - 10 * DAY, NOW)]
    gap = health.explain_gap(db.conn, NOW - 5 * DAY, NOW - 4 * DAY)
    assert gap["cause"] == "storage"
    assert health.explain_gap(db.conn, NOW - 20 * DAY, NOW - 19 * DAY)["cause"] != "storage"
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, 'storage_ended', 'info', '{}')",
                    (NOW - 2 * DAY,))
    assert storage.spans(db.conn, NOW) == [(NOW - 10 * DAY, NOW - 2 * DAY)]
    assert storage.stored_during(db.conn, NOW - 12 * DAY, NOW - 2 * DAY, NOW) == pytest.approx(0.8)


# -- the suggestion -------------------------------------------------------------------

def test_suggestion(db):
    db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, source) VALUES (?, 1, 80, 'mqtt')",
                    (NOW - 30 * DAY,))
    db.conn.execute("INSERT INTO drives (start_ts, end_ts, source, metrics, created_ts) VALUES (?, ?, 'x', '{}', 0)",
                    (NOW - 12 * DAY - 3600, NOW - 12 * DAY))
    assert storage.suggestion(db.conn, NOW, storage.State()) == "No drive for 12 days."
    assert storage.suggestion(db.conn, NOW, storage.State(on=True)) is None
    assert storage.suggestion(db.conn, NOW - 10 * DAY, storage.State()) is None     # drove 2 days before


def test_no_suggestion_for_a_new_box(db):
    db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, source) VALUES (?, 1, 80, 'mqtt')",
                    (NOW - 2 * DAY,))
    assert storage.suggestion(db.conn, NOW, storage.State()) is None


# -- the collector --------------------------------------------------------------------

def _collector(db, cfg):
    return types.SimpleNamespace(db=db, config_path=str(cfg), last_storage_tick_ts=0,
                                 cfg=types.SimpleNamespace(bank=types.SimpleNamespace(charge_on_a=20.0)))


def _events(db):
    return [(t, json.loads(d)) for t, d in db.conn.execute(
        "SELECT type, detail FROM events WHERE type LIKE 'storage_%' ORDER BY rowid")]


def test_collector_records_changes(db, cfg):
    c = _collector(db, cfg)
    Cerberus._storage_tick(c)
    assert _events(db) == [] and db.get_kv(KV_STORAGE).startswith("off")   # first look, off: nothing to say
    storage.start(cfg, int(time.time()), until="2027-03-01")
    c.last_storage_tick_ts = 0
    Cerberus._storage_tick(c)
    c.last_storage_tick_ts = 0
    Cerberus._storage_tick(c)                                         # no change: no second event
    storage.end(cfg, int(time.time()))
    c.last_storage_tick_ts = 0
    Cerberus._storage_tick(c)
    kinds = [k for k, _ in _events(db)]
    assert kinds == ["storage_started", "storage_ended"]


def test_a_drive_ends_storage(db, cfg):
    now = int(time.time())
    storage.start(cfg, now - DAY)
    db.set_kv("live.snapshot", json.dumps({"ts": now, "bank": {"current": 45.0}, "solar": {"charge_a": 0},
                                           "ac": {}}))
    c = _collector(db, cfg)
    Cerberus._storage_tick(c)
    s = storage.read(cfg)
    assert not s.on and s.by == "drive" and s.ended_reason.startswith("drive: the engine is charging")
    # It began and ended unwatched: both are recorded, with their real times.
    rows = db.conn.execute("SELECT type, ts FROM events WHERE type LIKE 'storage_%' ORDER BY rowid").fetchall()
    assert rows == [("storage_started", now - DAY), ("storage_ended", s.changed_at)]
    assert storage.spans(db.conn, now) == [(now - DAY, s.changed_at)]
    c.last_storage_tick_ts = 0
    Cerberus._storage_tick(c)                       # and only once
    assert len(_events(db)) == 2


# -- the UI ---------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    cfgp = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfgp)
    d = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfgp), d._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfgp, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    d.close()


def _call(port, method, path, body=None, token=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, "/api/v1" + path, body=json.dumps(body) if body is not None else None,
              headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    r = c.getresponse()
    return r.status, json.loads(r.read())


def test_storage_through_the_api(server):
    cfgp, port = server
    assert _call(port, "GET", "/storage")[1]["on"] is False
    assert _call(port, "POST", "/storage", {"on": True})[0] == 401
    auth.set_pin(cfgp, "2468")
    tok = _call(port, "POST", "/auth/login", {"pin": "2468"})[1]["token"]
    assert _call(port, "POST", "/storage", {"on": True, "until": "someday"}, tok)[0] == 400
    assert _call(port, "POST", "/storage", {}, tok)[0] == 400
    code, r = _call(port, "POST", "/storage", {"on": True, "until": "2027-03-01", "online": True}, tok)
    assert code == 200 and r["storage"]["on"] and r["storage"]["until"] == "2027-03-01" and r["storage"]["online"]
    v = _call(port, "GET", "/storage")[1]
    assert v["on"] is True and v["due_passed"] is False and v["suggest"] is None
    # A stored, parked van: the phone's position isn't used.
    code, r = _call(port, "POST", "/location/phone",
                    {"lat": 12.35, "lon": -98.77, "accuracy_m": 10, "ts_ms": int(time.time() * 1000)}, tok)
    assert code == 200 and r["accepted"] is False and "storage" in r["reason"]
    assert _call(port, "POST", "/storage", {"on": False}, tok)[1]["storage"]["on"] is False
