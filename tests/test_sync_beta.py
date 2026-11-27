"""Sync for the beta (Core #43): what is sent, in what order, and what never leaves the box.

test_syncer.py proves the store-and-forward contract (no losses, no duplicates). This adds:
- configurable tables, with the beta set;
- every batch says which box and which key columns, so re-sends are harmless;
- positions never leave the box unless sharing them is on, including inside JSON;
- finding_log changes (cleared, reopened, escalated) are sent, the per-tick refresh isn't;
- events and findings first; samples one batch per table per tick; metered holds high-rate data;
- the owner's notes as one document, on change;
- the UI shows what's waiting and what was sent, never the token.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import notes
from cerberus import syncer as syncer_mod
from cerberus.config import BETA_SYNC_TABLES, SyncConfig
from cerberus.db import Db
from cerberus.events import Event, Severity
from cerberus.syncer import Syncer, strip_position
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
DEVICE = "3f0c8a52-6d1e-4b7a-9a43-0d2a6c1e9b11"


@pytest.fixture(autouse=True)
def _cerbo_password(monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "c.db"))
    yield d
    d.close()


def _cfg(tables=("events", "finding_log", "battery_samples", "weather_samples"), batch=100, **kw):
    return SyncConfig(enabled=True, target_url="http://example.test/ingest", auth_token="tok",
                      batch_size=batch, interval_seconds=60, request_timeout_seconds=5,
                      tables=tuple(tables), **kw)


def _capture(s: Syncer):
    sent = []

    def fake(table, rows):
        sent.append((table, [dict(r) for r in rows]))
        return True
    return sent, patch.object(s, "_post", side_effect=fake)


def _finding(db, key="low_soc", severity="warn", ts=1000):
    db.conn.execute("INSERT INTO finding_log (key, title, severity, opened_ts, last_seen, explain) "
                    "VALUES (?, 'Low charge', ?, ?, ?, 'x')", (key, severity, ts, ts))
    return db.conn.execute("SELECT MAX(num) FROM finding_log").fetchone()[0]


# -- config -----------------------------------------------------------------------

def _load(tmp_path, sync):
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    raw["sync"] = {**raw.get("sync", {}), **sync}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return cfg_mod.load(str(p)).sync


def test_tables_config(tmp_path):
    assert _load(tmp_path, {}).tables == ("battery_samples", "events")
    assert _load(tmp_path, {"tables": "beta"}).tables == BETA_SYNC_TABLES
    assert _load(tmp_path, {"tables": ["events", "events", "drives"]}).tables == ("events", "drives")
    for bad in ({"tables": ["sync_state"]}, {"tables": []}, {"tables": "everything"}, {"prompt_seconds": 1}):
        with pytest.raises(cfg_mod.ConfigError):
            _load(tmp_path, bad)
    c = _load(tmp_path, {"share_position": True, "metered": True})
    assert c.share_position and c.metered and not _load(tmp_path, {}).share_position


def test_every_beta_table_is_syncable_and_has_a_key():
    for t in BETA_SYNC_TABLES:
        assert Db.SYNC_KEYS[t]


# -- the batch ----------------------------------------------------------------------

def test_payload_says_which_box_and_which_key(db):
    s = Syncer(_cfg(), db, device_id=DEVICE, core_version="0.1.0")
    body = s.payload("battery_samples", [{"ts": 1, "pack_id": 1}])
    assert body["format"] == "cerberus.sync" and body["version"] == 1
    assert body["box"] == {"id": DEVICE, "core_version": "0.1.0"}
    assert body["table"] == "battery_samples" and body["key"] == ["ts", "pack_id"]
    assert s.payload("events", [])["key"] == ["rowid"]


def test_events_carry_their_rowid_as_key(db):
    for i in range(3):
        db.insert_event(Event(ts=100 + i, type="t", severity=Severity.INFO, detail={}))
    s = Syncer(_cfg(tables=("events",)), db)
    sent, p = _capture(s)
    with p:
        s.tick()
    rows = sent[0][1]
    assert [r["rowid"] for r in rows] == [1, 2, 3]


# -- position ----------------------------------------------------------------------

def _seed_positions(db):
    db.conn.execute("INSERT INTO weather_samples (ts, air_temp_f, lat, lon, position_source) "
                    "VALUES (10, 40.1, 12.3456, -98.7654, 'gps')")
    db.insert_event(Event(ts=11, type="phone_fix", severity=Severity.INFO,
                          detail={"lat": 12.3456, "lon": -98.7654, "nested": {"latitude": 12.3456, "ok": 1}}))


def test_no_position_leaves_the_box_by_default(db):
    _seed_positions(db)
    s = Syncer(_cfg(), db)
    sent, p = _capture(s)
    with p:
        s.tick()
    text = json.dumps(sent)
    for leak in ("12.34", "-98.76", '"lat"', '"lon"', "latitude", "position_source"):
        assert leak not in text, leak
    weather = next(rows for t, rows in sent if t == "weather_samples")
    assert weather[0]["air_temp_f"] == 40.1
    event = next(rows for t, rows in sent if t == "events")
    assert json.loads(event[0]["detail"])["detail"]["nested"] == {"ok": 1}


def test_positions_go_when_the_owner_shares_them(db):
    _seed_positions(db)
    s = Syncer(_cfg(share_position=True), db)
    sent, p = _capture(s)
    with p:
        s.tick()
    assert "12.3456" in json.dumps(sent)


def test_strip_position_leaves_other_json_alone():
    row = {"metrics": json.dumps({"start": {"lat": 1, "soc": 80}, "km": 12}), "note": "{not json"}
    out = strip_position(row)
    assert json.loads(out["metrics"]) == {"km": 12, "start": {"soc": 80}}
    assert out["note"] == "{not json"


# -- findings -------------------------------------------------------------------------

def test_finding_changes_are_sent_and_refreshes_are_not(db):
    s = Syncer(_cfg(tables=("finding_log",)), db)
    sent, p = _capture(s)
    num = _finding(db)
    with p:
        s.tick()
        assert [r["num"] for r in sent[-1][1]] == [num] and "sync_seq" not in sent[-1][1][0]
        n = len(sent)
        # the per-tick refresh (last_seen, explain, same severity) is not a change
        db.conn.execute("UPDATE finding_log SET title = 'Low charge', severity = 'warn', last_seen = 2000, "
                        "explain = 'y' WHERE num = ?", (num,))
        s.tick()
        assert len(sent) == n
        db.conn.execute("UPDATE finding_log SET severity = 'crit' WHERE num = ?", (num,))   # escalated
        s.tick()
        assert sent[-1][1][0]["severity"] == "crit"
        db.conn.execute("UPDATE finding_log SET cleared_ts = 3000 WHERE num = ?", (num,))   # cleared
        s.tick()
        assert sent[-1][1][0]["cleared_ts"] == 3000
        db.conn.execute("UPDATE finding_log SET cleared_ts = NULL WHERE num = ?", (num,))   # reopened
        s.tick()
        assert sent[-1][1][0]["cleared_ts"] is None


def test_existing_findings_are_numbered_on_upgrade(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE finding_log (num INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, "
                 "title TEXT NOT NULL, severity TEXT NOT NULL, opened_ts INTEGER NOT NULL, cleared_ts INTEGER, "
                 "last_seen INTEGER, explain TEXT, prev_num INTEGER)")
    for i in range(3):
        conn.execute("INSERT INTO finding_log (key, title, severity, opened_ts) VALUES ('k', 't', 'warn', ?)", (i,))
    conn.commit()
    conn.close()
    d = Db(str(path))
    try:
        assert [r[0] for r in d.conn.execute("SELECT sync_seq FROM finding_log ORDER BY num")] == [1, 2, 3]
        _finding(d)
        assert d.conn.execute("SELECT MAX(sync_seq) FROM finding_log").fetchone()[0] == 4
    finally:
        d.close()


# -- order and pace -------------------------------------------------------------------

def test_events_and_findings_first_and_drained(db):
    for i in range(250):
        db.insert_event(Event(ts=i, type="t", severity=Severity.INFO, detail={}))
    _finding(db)
    db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, source) VALUES (1, 1, 80, 'mqtt')")
    s = Syncer(_cfg(batch=100), db)
    sent, p = _capture(s)
    with p:
        s.tick(samples=False)
    assert [t for t, _ in sent] == ["events", "events", "events", "finding_log"]
    assert sum(len(r) for t, r in sent if t == "events") == 250
    with p:
        s.tick()
    assert ("battery_samples", ) == tuple(t for t, _ in sent[4:5])


def test_samples_one_batch_per_table_per_tick(db):
    for i in range(30):
        db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, source) VALUES (?, 1, 80, 'mqtt')", (i,))
    s = Syncer(_cfg(tables=("battery_samples",), batch=10), db)
    sent, p = _capture(s)
    with p:
        s.tick()
    assert [len(r) for _, r in sent] == [10]


def test_metered_holds_high_rate_tables(db):
    s = Syncer(_cfg(tables=("events", "battery_samples", "cell_samples"), metered=True), db)
    assert s.sample_tables() == ["battery_samples"]
    assert s.sample_interval == 4 * 60
    db.conn.execute("INSERT INTO cell_samples (ts, pack_id, cell_idx, voltage_mv) VALUES (5, 1, 1, 3300)")
    held = next(w for w in s.waiting() if w["table"] == "cell_samples")
    assert held == {"table": "cell_samples", "waiting": 1, "oldest_ts": 5, "held": True}


# -- notes ------------------------------------------------------------------------------

def test_notes_go_as_one_document_on_change(db, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    notes.add(cfg, "low_soc", "Fridge left on", now=100)
    s = Syncer(_cfg(tables=("events",)), db, device_id=DEVICE, notes_path=notes.path_for(cfg))
    bodies = []
    with patch.object(s, "_send", side_effect=lambda body, what: bodies.append(body) or True):
        s.tick()
        s.tick()                                   # unchanged: not sent again
        notes.add(cfg, "low_soc", "Replaced the fridge fuse", now=200)
        s.tick()
    docs = [b for b in bodies if b.get("kind") == "notes"]
    assert len(docs) == 2 and docs[0]["box"]["id"] == DEVICE
    assert [n["text"] for n in docs[1]["document"]] == ["Fridge left on", "Replaced the fridge fuse"]


def test_notes_retry_after_a_failure(db, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    notes.add(cfg, "k", "a note", now=1)
    s = Syncer(_cfg(tables=("events",)), db, notes_path=notes.path_for(cfg))
    tries = []
    with patch.object(s, "_send", side_effect=lambda body, what: tries.append(what) or len(tries) > 1):
        s.tick()
        s.tick()
        s.tick()
    assert tries == ["notes", "notes"]


# -- the van's profile (cloud #23) -------------------------------------------------------

def test_profile_goes_as_one_document_on_change(db, tmp_path):
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw))
    s = Syncer(_cfg(tables=("events",)), db, device_id=DEVICE)
    s.config_path = cfg
    db.set_kv("van.timezone", "America/Denver")
    bodies = []
    with patch.object(s, "_send", side_effect=lambda body, what: bodies.append(body) or True):
        s.tick()
        s.tick()                                   # unchanged: not sent again
        raw.setdefault("bank", {})["capacity_ah"] = 450
        cfg.write_text(yaml.safe_dump(raw))
        s.tick()
    docs = [b["document"] for b in bodies if b.get("kind") == "profile"]
    assert len(docs) == 2
    want = cfg_mod.load_ui(cfg)
    assert docs[1]["capacity_ah"] == 450 and docs[1]["pack_count"] == want.pack_count
    assert docs[0]["time_zone"] == (want.timezone or "America/Denver")
    assert set(docs[0]["data_sources"]) >= {"victron_gx_mqtt", "battery_bluetooth"}
    flat = json.dumps(docs)
    for leak in ("lat", "lon", "password", "token", "host"):
        assert f'"{leak}' not in flat


def test_no_config_no_profile_and_a_bad_one_waits(db, tmp_path):
    s = Syncer(_cfg(tables=("events",)), db)
    bodies = []
    with patch.object(s, "_send", side_effect=lambda body, what: bodies.append(body) or True):
        s.tick()                                   # no config path: nothing
        bad = tmp_path / "config.yaml"
        bad.write_text("bank: [not, a, mapping")
        s.config_path = bad
        s.tick()                                   # unreadable: skipped, tried again later
    assert not [b for b in bodies if b.get("kind") == "profile"]


# -- on the wire, and in the UI ---------------------------------------------------------

class _Sink:
    def __init__(self):
        self.got = []
        sink = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                sink.got.append((self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/cerberus/sync"


def test_real_post_and_the_settings_view(tmp_path):
    sink = _Sink()
    try:
        raw = yaml.safe_load(REPO_CONFIG.read_text())
        raw["sync"] = {**raw["sync"], "enabled": True, "target_url": sink.url, "auth_token": "sync-secret",
                       "tables": ["events", "battery_samples"]}
        cfgp = tmp_path / "config.yaml"
        cfgp.write_text(yaml.safe_dump(raw))
        db = Db(str(tmp_path / "s.db"))
        db.insert_event(Event(ts=5, type="t", severity=Severity.INFO, detail={}))
        for i in range(3):
            db.conn.execute("INSERT INTO battery_samples (ts, pack_id, soc, source) VALUES (?, 1, 80, 'mqtt')", (i,))
        s = Syncer(cfg_mod.load(str(cfgp)).sync, db, device_id=DEVICE, core_version="0.1.0")
        s.tick(samples=False)                    # events only: the samples wait
        auth, body = sink.got[0]
        assert auth == "Bearer sync-secret" and body["table"] == "events" and body["box"]["id"] == DEVICE

        httpd = serve(App.from_config(str(cfgp), db._path), "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/v1/sync")
            resp = c.getresponse()
            text = resp.read().decode()
            v = json.loads(text)
            assert resp.status == 200 and v["enabled"] is True and "sync-secret" not in text
            waiting = {w["table"]: w["waiting"] for w in v["waiting"]}
            assert waiting == {"events": 0, "battery_samples": 3}
            assert v["last"]["ok"] is True and v["last"]["sent"] == {"events": 1}
        finally:
            httpd.shutdown()
            httpd.server_close()
            db.close()
    finally:
        sink.httpd.shutdown()
        sink.httpd.server_close()


def test_settings_switches_exist():
    from cerberus import settings
    keys = {s.key for s in settings.SETTINGS}
    assert {"sync.enabled", "sync.share_position", "sync.metered"} <= keys
