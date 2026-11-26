"""The settings PIN can't be guessed: attempts count before the check, per requester and box-wide."""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def sessions(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    auth.set_pin(cfg, "2468")
    clock = Clock()
    return auth.Sessions(cfg, clock=clock), clock


def test_parallel_guesses_cannot_slip_under_the_limit(sessions, monkeypatch):
    s, _ = sessions
    checked = []

    def slow_wrong(path, pin):                     # a slow check, so the guesses really overlap
        checked.append(pin)
        time.sleep(0.2)
        return False
    monkeypatch.setattr(auth, "check_pin", slow_wrong)
    errors = []

    def guess(i):
        try:
            s.login(f"{i:04d}", "192.0.2.7")
        except auth.AuthError as e:
            errors.append(str(e))
    threads = [threading.Thread(target=guess, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checked) <= auth.MAX_FAILURES                  # was all 30 before
    assert sum("Too many" in e for e in errors) >= 30 - auth.MAX_FAILURES


def test_many_addresses_hit_the_box_wide_cap(sessions):
    s, _ = sessions
    for i in range(auth.BOX_MAX_FAILURES):
        with pytest.raises(auth.AuthError, match="Wrong PIN"):
            s.login("0000", f"192.0.2.{i + 1}")
    with pytest.raises(auth.AuthError, match="on this box"):
        s.login("2468", "198.51.100.1")                       # even the right PIN waits it out


def test_a_right_pin_takes_its_attempt_back_and_windows_expire(sessions):
    s, clock = sessions
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            s.login("0000", "192.0.2.9")
    assert s.login("2468", "192.0.2.9")                       # the 5th attempt is right: in
    assert "192.0.2.9" not in s._fails
    with pytest.raises(auth.AuthError):
        s.login("0000", "192.0.2.10")
    clock.t += auth.BOX_LOCKOUT_S + 1
    with pytest.raises(auth.AuthError, match="Wrong PIN"):
        s.login("0000", "192.0.2.11")
    assert list(s._fails) == ["192.0.2.11"] and len(s._all_fails) == 1     # swept


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "test-only")
    cfgp = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, cfgp)
    auth.set_pin(cfgp, "2468")
    d = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfgp), d._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    d.close()


def _login(base, pin, **headers):
    r = urllib.request.Request(base + "/api/v1/auth/login", data=json.dumps({"pin": pin}).encode(), method="POST",
                               headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_relayed_people_each_have_their_own_lockout(server):
    a = {"X-Cerberus-Via": "relay", "X-Cerberus-Relay-User": "person-a", "Host": "x.box.example"}
    b = {"X-Cerberus-Via": "relay", "X-Cerberus-Relay-User": "person-b", "Host": "x.box.example"}
    for _ in range(auth.MAX_FAILURES):
        assert _login(server, "0000", **a)[0] == 403
    code, r = _login(server, "2468", **a)
    assert code == 403 and "Too many" in r["error"]            # person A is locked out...
    code, r = _login(server, "2468", **b)
    assert code == 200 and r["token"]                          # ...person B isn't
