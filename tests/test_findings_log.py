"""Numbered findings and how each one ended (cerberus.findings_log): every
occurrence keeps its number, and a cleared one waits for the owner to say what
fixed it."""

from __future__ import annotations

import pytest

from cerberus import findings as F
from cerberus import findings_log as L
from cerberus import notes
from cerberus.db import Db
from cerberus.ui import api

NOW = 1794000000
DAY = 86400


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "l.db"))


def _f(key, status=F.OPEN, sev=F.ATTENTION, first=NOW - DAY, last=NOW - 60, title=None):
    return F.Finding(key=key, severity=sev, status=status, title=title or key.replace("_", " "),
                     explain=f"{key} explained", action="do something",
                     first_seen=first, last_seen=last)


def _states(db, ns=None):
    return [(r["num"], r["key"], r["state"]) for r in L.history(db.conn, ns or [])]


def test_an_open_finding_gets_a_number_and_keeps_it(db):
    assert L.sync(db.conn, [_f("solar_path_resistance")], NOW) == [("opened", 1, "solar_path_resistance")]
    assert L.sync(db.conn, [_f("solar_path_resistance", sev=F.URGENT, title="Worse")], NOW + 300) == []
    (r,) = L.history(db.conn)
    assert (r["num"], r["state"], r["severity"], r["title"]) == (1, L.OPEN, F.URGENT, "Worse")
    # Severity keeps the highest it reached.
    L.sync(db.conn, [_f("solar_path_resistance", sev=F.INFO)], NOW + 600)
    assert L.history(db.conn)[0]["severity"] == F.URGENT


def test_clearing_waits_for_a_note_and_a_resolution_closes_it(db):
    L.sync(db.conn, [_f("alternator_overshoot")], NOW)
    assert L.sync(db.conn, [_f("alternator_overshoot", status=F.RESOLVED)], NOW + 300) == [
        ("cleared", 1, "alternator_overshoot")]
    assert _states(db) == [(1, "alternator_overshoot", L.NEEDS_NOTE)]
    plain = {"id": "a", "key": "alternator_overshoot", "ts": NOW + 400, "text": "hm", "num": 1, "kind": "note"}
    assert _states(db, [plain]) == [(1, "alternator_overshoot", L.NEEDS_NOTE)]
    fix = {**plain, "id": "b", "kind": "resolution", "text": "ACR limits set"}
    (r,) = L.history(db.conn, [plain, fix])
    assert r["state"] == L.CLOSED and r["resolution"]["text"] == "ACR limits set"


def test_a_resolution_before_the_data_clears_is_fix_noted(db):
    L.sync(db.conn, [_f("alternator_overshoot")], NOW)
    fix = {"id": "b", "key": "alternator_overshoot", "ts": NOW + 10, "text": "ACR limits",
           "num": 1, "kind": "resolution"}
    assert _states(db, [fix]) == [(1, "alternator_overshoot", L.FIX_NOTED)]
    L.sync(db.conn, [], NOW + 300)                     # gone from derive altogether
    assert _states(db, [fix]) == [(1, "alternator_overshoot", L.CLOSED)]


def test_coming_back_is_a_new_number_linked_to_the_last_unless_it_flickered(db):
    L.sync(db.conn, [_f("k")], NOW)
    L.sync(db.conn, [], NOW + 300)
    assert L.sync(db.conn, [_f("k")], NOW + 600) == [("reopened", 1, "k")]       # within a day
    L.sync(db.conn, [], NOW + 900)
    later = NOW + 900 + L.REOPEN_GRACE_S + 1
    assert L.sync(db.conn, [_f("k", first=NOW - DAY)], later) == [("opened", 2, "k")]
    new = L.history(db.conn)[0]
    assert new["prev_num"] == 1 and new["opened_ts"] == NOW + 900   # not before the last cleared


def test_already_resolved_findings_are_logged_once_as_cases(db):
    old = _f("soc_reads_high", status=F.RESOLVED, first=NOW - 20 * DAY, last=NOW - 3 * DAY)
    assert L.sync(db.conn, [old], NOW) == [("cleared", 1, "soc_reads_high")]
    assert L.sync(db.conn, [old], NOW + 300) == []
    (r,) = L.history(db.conn)
    assert (r["state"], r["opened_ts"], r["cleared_ts"]) == (L.NEEDS_NOTE, NOW - 20 * DAY, NOW - 3 * DAY)


def test_notes_from_before_numbers_find_their_occurrence(db):
    L.sync(db.conn, [_f("k", first=NOW - DAY)], NOW)
    L.sync(db.conn, [], NOW + 300)
    L.sync(db.conn, [_f("k")], NOW + 5 * DAY)
    rows = L._rows(db.conn)
    assert L.note_num({"key": "k", "ts": NOW - 100}, rows) == 1
    assert L.note_num({"key": "k", "ts": NOW + 6 * DAY}, rows) == 2
    assert L.note_num({"key": "k", "ts": NOW - 9 * DAY}, rows) == 1          # before any: the first
    assert L.note_num({"key": "other", "ts": NOW}, rows) is None
    assert L.note_num({"key": "k", "ts": NOW, "num": 7}, rows) == 7


def test_notes_file_takes_a_number_and_a_resolution(tmp_path):
    cfg = tmp_path / "config.yaml"
    n = notes.add(cfg, "k", "Tightened the lugs", num=3, kind="resolution", now=5)
    assert (n["num"], n["kind"]) == (3, "resolution")
    assert notes.read(cfg)[0]["num"] == 3 and notes.read(cfg)[0]["kind"] == "resolution"
    assert notes.add(cfg, "k", "plain", now=6)["kind"] == "note"
    for bad in ({"kind": "resolution"}, {"kind": "guess", "num": 1}, {"num": "x"}, {"num": 0}):
        with pytest.raises(notes.NoteError):
            notes.add(cfg, "k", "x", **bad)


def test_a_database_from_before_the_log_has_no_history(tmp_path):
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "old.db"))
    assert L.history(c) == []


# --- the views -----------------------------------------------------------------------

def test_findings_view_numbers_and_lists_what_waits_for_a_note(db):
    db.conn.execute("INSERT INTO events (ts, type, severity, detail) VALUES (?, 'solar_output_open', "
                    "'warn', '{\"detail\": {\"pv_voltage\": 20}}')", (NOW - 60,))
    found = F.derive(db.conn, F.Settings(), NOW)
    L.sync(db.conn, found, NOW)
    # Each pass sees everything derive gives; these two had already resolved.
    L.sync(db.conn, found + [_f("early_float", status=F.RESOLVED, first=NOW - 30 * DAY, last=NOW - 20 * DAY)], NOW)
    L.sync(db.conn, found + [_f("pack_soc_spread", status=F.RESOLVED, first=NOW - 40 * DAY, last=NOW - 30 * DAY)], NOW)
    nums = {r["key"]: r["num"] for r in L.history(db.conn)}
    fixed = {"id": "x", "key": "pack_soc_spread", "ts": NOW, "text": "Balanced",
             "num": nums["pack_soc_spread"], "kind": "resolution", "title": "", "known": False}
    v = api.findings_view(db.conn, api.Context(), NOW, [fixed])
    f = next(f for f in v["findings"] if f["key"] == "solar_output_open")
    assert (f["num"], f["state"]) == (nums["solar_output_open"], L.OPEN)
    assert [r["key"] for r in v["awaiting"]] == ["early_float"]
    assert [r["key"] for r in v["closed"]] == ["pack_soc_spread"]
    assert v["numbered"] and v["other_notes"] == []
    now = api.now_view(db.conn, api.Context(), NOW, [fixed])
    assert now["needs_note_count"] == 1
    assert next(x for x in now["findings"] if x["key"] == "solar_output_open")["num"] == nums["solar_output_open"]
    log = api.findings_log_view(db.conn, api.Context(), [fixed])["occurrences"]
    assert [r["num"] for r in log] == sorted(nums.values(), reverse=True)
