"""Home ZIP code -> the latitude/longitude weather uses (cerberus.geocode).
Offline: the lookup service is faked."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from cerberus import config as cfg_mod
from cerberus import geocode, settings
from cerberus.db import Db
from cerberus.ui import auth
from cerberus.ui.server import App, serve

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
BOULDER = {"post code": "80302", "places": [
    {"place name": "Boulder", "longitude": "-105.2705", "latitude": "40.015",
     "state": "Colorado", "state abbreviation": "CO"}]}


def fake_fetch(url):
    return BOULDER if url.endswith("/80302") else None      # None = the service's 404


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(REPO_CONFIG, p)
    return p


@pytest.mark.parametrize("given, want", [("80302", "80302"), (" 80302-1234 ", "80302"), (1234, None),
                                         ("0796", None), ("ABCDE", None), ("", None)])
def test_normalize_zip(given, want):
    if want:
        assert geocode.normalize_zip(given) == want
    else:
        with pytest.raises(geocode.GeocodeError):
            geocode.normalize_zip(given)


def test_lookup():
    assert geocode.lookup_zip("80302-0001", fetch=fake_fetch) == {
        "zip": "80302", "latitude": 40.015, "longitude": -105.2705, "place": "Boulder, CO"}
    with pytest.raises(geocode.GeocodeError, match="wasn't found"):
        geocode.lookup_zip("00000", fetch=fake_fetch)


def test_a_zip_brings_its_position_and_saves(cfg):
    changes, notice = settings.expand_zip({"weather.zip_code": "80302"},
                                          lookup=lambda z: geocode.lookup_zip(z, fetch=fake_fetch))
    assert changes == {"weather.zip_code": "80302", "weather.latitude": 40.015,
                       "weather.longitude": -105.2705}
    assert "Boulder, CO" in notice
    settings.apply(cfg, changes)
    w = cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x").weather
    assert (w.zip_code, w.latitude, w.longitude) == ("80302", 40.015, -105.2705)
    assert yaml.safe_load(cfg_mod.setup_path(cfg).read_text())["weather"]["zip_code"] == "80302"


def test_no_zip_means_no_lookup():
    def boom(_):
        raise AssertionError("looked up")
    assert settings.expand_zip({"solar.array_w": 400}, lookup=boom) == ({"solar.array_w": 400}, None)
    assert settings.expand_zip({"weather.zip_code": ""}, lookup=boom)[1] is None


def test_unknown_zip_is_refused(cfg):
    with pytest.raises(cfg_mod.ConfigError, match="Home ZIP code"):
        settings.expand_zip({"weather.zip_code": "00000"},
                            lookup=lambda z: geocode.lookup_zip(z, fetch=fake_fetch))


def test_a_bad_zip_in_config_does_not_load(cfg):
    cfg.write_text(cfg.read_text().replace("weather:\n", "weather:\n  zip_code: 123\n", 1))
    with pytest.raises(cfg_mod.ConfigError, match="zip_code"):
        cfg_mod.from_raw(cfg_mod.read_raw(cfg), password="x")


def test_saving_a_zip_through_the_api(cfg, tmp_path, monkeypatch):
    real = geocode.lookup_zip
    monkeypatch.setattr(geocode, "lookup_zip", lambda z: real(z, fetch=fake_fetch))
    db = Db(str(tmp_path / "s.db"))
    httpd = serve(App.from_config(str(cfg), db._path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    try:
        auth.set_pin(cfg, "2468")

        def post(path, body, tok=None):
            r = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                       headers={"Content-Type": "application/json",
                                                **({"Authorization": f"Bearer {tok}"} if tok else {})})
            try:
                with urllib.request.urlopen(r, timeout=5) as x:
                    return x.status, json.loads(x.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        tok = post("/auth/login", {"pin": "2468"})[1]["token"]
        code, r = post("/settings", {"changes": {"weather.zip_code": "80302"}}, tok)
        assert code == 200 and "Boulder" in r["notice"]
        rows = {x["key"]: x for x in r["settings"]}
        assert rows["weather.latitude"]["value"] == 40.015 and rows["weather.zip_code"]["source"] == "setup"
        code, err = post("/settings", {"changes": {"weather.zip_code": "12"}}, tok)
        assert code == 400 and "ZIP" in err["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
