"""The box's own Wi-Fi network (Core #53), without root or a Wi-Fi card: fake commands and processes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cerberus import phonefix, wifiap
from cerberus.secretstore import SecretStore

IW_DEV = """phy#0
\tInterface wlo1
\t\tifindex 4
\t\ttype managed
"""
IW_LIST_AP = """Wiphy phy0
\tSupported interface modes:
\t\t * IBSS
\t\t * managed
\t\t * AP
\t\t * monitor
\tBand 1:
"""
IW_LIST_NO_AP = IW_LIST_AP.replace("\t\t * AP\n", "")


def test_settings_defaults_and_refusals():
    s = wifiap.settings_from({})
    assert (s.enabled, s.channel, s.address, s.interface, s.country) == (True, 6, "10.42.0.1/24", "", "US")
    assert wifiap.settings_from({"wifi_ap": {"enabled": "off"}}).enabled is False
    for bad in ({"channel": 3}, {"channel": "x"}, {"address": "8.8.8.8/24"}, {"address": "10.0.0.1/16"},
                {"interface": "wlo1; rm -rf /"}, {"country": "USA"}):
        with pytest.raises(wifiap.WifiError):
            wifiap.settings_from({"wifi_ap": bad})


def test_name_password_and_interface():
    assert wifiap.ssid("0a1b2c3d4e5f60718293a4b5c6d7e8f9") == "Cerberus-E8F9"
    assert wifiap.ssid("") == "Cerberus-BOX"
    pw = wifiap.new_password()
    assert len(pw) == 12 and set(pw) <= set(wifiap.ALPHABET)
    assert wifiap.ap_interface(IW_DEV) == "wlo1"
    assert wifiap.ap_interface(IW_DEV, "wlan9") is None
    assert wifiap.supports_ap(IW_LIST_AP) and not wifiap.supports_ap(IW_LIST_NO_AP)


def test_password_is_made_once(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    first = wifiap.password(store)
    assert wifiap.password(store) == first and store.get(wifiap.SECRET) == first


def test_hostapd_conf_and_rules():
    s = wifiap.Settings(channel=11)
    wpa3 = wifiap.hostapd_conf(s, "wlo1", "Cerberus-ABCD", "pw123456789x", True, "/run/x")
    assert "ssid=Cerberus-ABCD" in wpa3 and "channel=11" in wpa3 and "wpa_key_mgmt=WPA-PSK SAE" in wpa3
    assert "sae_require_mfp=1" in wpa3 and "hw_mode=g" in wpa3
    assert "wpa_key_mgmt=WPA-PSK\n" in wifiap.hostapd_conf(s, "wlo1", "n", "p" * 12, False, "/run/x")
    rules = wifiap.nft_ruleset("wlo1")
    for line in ('iifname "wlo1" tcp dport 8088 accept', 'iifname "wlo1" udp dport 67 accept',
                 'iifname "wlo1" drop', 'oifname "wlo1" drop', "hook forward priority -10"):
        assert line in rules
    args = wifiap.dnsmasq_args(s, "wlo1", Path("/run/x"))
    assert "--dhcp-range=10.42.0.10,10.42.0.50,255.255.255.0,12h" in args
    assert "--dhcp-option=3" in args and "--dhcp-option=6" in args and "--port=0" in args


class Fakes:
    """`ip`, `nft`, `iw` and the two daemons."""

    def __init__(self, iw_list=IW_LIST_AP, wpa3_ok=True):
        self.calls, self.inputs, self.procs = [], [], []
        self.iw_list, self.wpa3_ok = iw_list, wpa3_ok

    def run(self, args, check=False, capture_output=True, text=True, input=None):
        self.calls.append(args)
        if input is not None:
            self.inputs.append(input)
        out = {("iw", "dev"): IW_DEV, ("iw", "list"): self.iw_list}.get(tuple(args[:2]), "")
        if args[:2] == ["iw", "dev"] and "station" in args:
            out = "Station e2:2e:44:43:d6:d3 (on wlo1)\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    def popen(self, args, stdout=None, stderr=None):
        fake = self
        p = SimpleNamespace(args=args, code=None)
        p.poll = lambda: p.code
        p.terminate = lambda: setattr(p, "code", 0)
        p.kill = p.terminate
        p.wait = lambda timeout=None: 0
        if args[0] == "hostapd":
            conf = Path(args[1]).read_text()
            ok = fake.wpa3_ok or "SAE" not in conf
            stdout.write("wlo1: AP-ENABLED\n" if ok else "wlo1: AP-DISABLED\n")
            stdout.flush()
            if not ok:
                p.code = 1
        self.procs.append(p)
        return p


def _setup(tmp_path, wifi=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"wifi_ap": wifi} if wifi is not None else {}))
    (tmp_path / "machine-id").write_text("0123456789abcdef0123456789abcd1f\n")
    return cfg


def _stop_after_status(status_dir):
    return lambda: (status_dir / "status.json").exists()


def test_run_starts_isolates_reports_and_cleans_up(tmp_path):
    cfg, status_dir, f = _setup(tmp_path), tmp_path / "status", Fakes()
    code = wifiap.run(cfg, status_dir, run=f.run, popen=f.popen, machine_id_path=tmp_path / "machine-id",
                      stop=_stop_after_status(status_dir))
    assert code == 0
    assert ["ip", "addr", "add", "10.42.0.1/24", "dev", "wlo1"] in f.calls
    assert any('iifname "wlo1" drop' in i for i in f.inputs)                  # isolation applied
    assert [p.args[0] for p in f.procs] == ["hostapd", "dnsmasq"]
    assert f.calls[-3:] == [["nft", "delete", "table", "inet", "cerberus_wifi"],
                            ["ip", "addr", "flush", "dev", "wlo1"], ["ip", "link", "set", "wlo1", "down"]]
    pw = SecretStore.for_config(cfg).get(wifiap.SECRET)
    assert pw and pw not in json.dumps(wifiap.read_status(status_dir))
    assert pw not in (status_dir / "status.json").read_text()


def test_status_while_up_names_the_network_and_counts_phones(tmp_path):
    cfg, status_dir, f = _setup(tmp_path), tmp_path / "status", Fakes()
    seen = {}

    def stop():
        if (status_dir / "status.json").exists():
            seen.update(wifiap.read_status(status_dir))
            return True
        return False
    wifiap.run(cfg, status_dir, run=f.run, popen=f.popen, machine_id_path=tmp_path / "machine-id", stop=stop)
    assert (seen["state"], seen["ssid"], seen["address"], seen["security"], seen["clients"]) == \
           ("on", "Cerberus-CD1F", "10.42.0.1", "WPA2/WPA3", 1)
    assert wifiap.read_status(status_dir)["state"] == "off"                   # and off once stopped


def test_wpa3_refused_falls_back_to_wpa2(tmp_path):
    cfg, status_dir, f = _setup(tmp_path), tmp_path / "status", Fakes(wpa3_ok=False)
    seen = {}

    def stop():
        if (status_dir / "status.json").exists():
            seen.update(wifiap.read_status(status_dir))
            return True
        return False
    wifiap.run(cfg, status_dir, run=f.run, popen=f.popen, machine_id_path=tmp_path / "machine-id", stop=stop)
    assert seen["security"] == "WPA2"


def test_off_unavailable_and_bad_settings_say_why_without_starting(tmp_path):
    status_dir = tmp_path / "status"
    for wifi, iw_list, state in (({"enabled": False}, IW_LIST_AP, "off"), (None, IW_LIST_NO_AP, "unavailable"),
                                 ({"channel": 4}, IW_LIST_AP, "error")):
        f = Fakes(iw_list=iw_list)
        cfg = _setup(tmp_path, wifi)
        assert wifiap.run(cfg, status_dir, run=f.run, popen=f.popen, machine_id_path=tmp_path / "machine-id",
                          stop=lambda: True) == 0
        assert wifiap.read_status(status_dir)["state"] == state
        assert not f.procs


def test_a_daemon_dying_is_an_error_and_still_cleans_up(tmp_path):
    cfg, status_dir, f = _setup(tmp_path), tmp_path / "status", Fakes()
    ticks = {"n": 0}

    def stop():
        ticks["n"] += 1
        if ticks["n"] == 2:
            f.procs[-1].code = 1                                                # dnsmasq dies
        return False
    assert wifiap.run(cfg, status_dir, run=f.run, popen=f.popen, machine_id_path=tmp_path / "machine-id", stop=stop, poll_s=0.01) == 1
    assert wifiap.read_status(status_dir)["state"] == "error"
    assert ["nft", "delete", "table", "inet", "cerberus_wifi"] in f.calls[-3:]


def test_a_phone_on_the_box_network_is_with_the_van():
    assert phonefix.client_kind("10.42.0.43", {}) == phonefix.LAN


def test_join_code_escapes_and_draws():
    assert wifiap.join_uri("Cerberus-AB12", "Pw23456789ab") == "WIFI:T:WPA;S:Cerberus-AB12;P:Pw23456789ab;;"
    assert wifiap.join_uri("N", 'a\\b;c,d:e"f') == 'WIFI:T:WPA;S:N;P:a\\\\b\\;c\\,d\\:e\\"f;;'
    svg = wifiap.qr_svg(wifiap.join_uri("Cerberus-AB12", "Pw23456789ab"))
    assert svg.startswith("<svg") and "Pw23456789ab" not in svg                 # drawn, not written out


def test_name_and_label_password_are_kept(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    mid = tmp_path / "machine-id"
    mid.write_text("00000000000000000000000000001a2b\n")
    name, pw = wifiap.identity(store, mid)
    assert name == "Cerberus-1A2B" and store.get(wifiap.LABEL_SECRET) == pw and wifiap.on_label(store)
    mid.write_text("ffffffffffffffffffffffffffff9999\n")                   # an OS reinstall: the label stays right
    assert wifiap.identity(store, mid) == (name, pw)
    store.set(wifiap.SECRET, "Another12345")
    assert not wifiap.on_label(store)
    wifiap.use_label_password(store)
    assert store.get(wifiap.SECRET) == pw and wifiap.on_label(store)
    with pytest.raises(wifiap.WifiError):
        wifiap.use_label_password(SecretStore(tmp_path / "none"))


def test_label_command_draws_both_codes_and_the_password(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}")
    out = tmp_path / "label.svg"
    assert wifiap.main(["label", "--config", str(cfg), "--out", str(out)]) == 0
    svg = out.read_text()
    store = SecretStore.for_config(cfg)
    assert store.get(wifiap.SECRET) in svg and store.get(wifiap.SSID_SECRET) in svg
    assert svg.count("<svg") == 3 and "http://10.42.0.1:8088" in svg          # the label and its two codes
    assert (out.stat().st_mode & 0o077) == 0                                   # it holds the password
