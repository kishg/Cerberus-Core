"""The GX on the box's second Ethernet port (Core #53, part 2), without root, a second port or a GX:
a fake /sys/class/net, fake networkctl / ip / nft / systemctl, and fake GX answers for discovery."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from cerberus import config as cfg_mod
from cerberus import firstrun, gxlink, gxport

NETPLAN = "/run/systemd/network/10-netplan-enp2s0.network"
OURS = f"/run/systemd/network/{gxport.NETWORK_FILE}"


def _status_text(network_file=None, leases=()):
    lines = ["● 2: x", "         Link File: /usr/lib/systemd/network/99-default.link",
             f"      Network File: {network_file or 'n/a'}", "              Type: ether"]
    if leases:
        lines.append("Offered DHCP leases: " + "\n                     ".join(f"{a} (to id:01:02:00:00:00:00:01)" for a in leases))
        lines.append("     Activation Policy: up")
    return "\n".join(lines) + "\n"


class Box:
    """A fake box: wired ports in a fake sysfs, and the commands the service runs."""

    def __init__(self, tmp: Path, ports: dict, networkd=True, nm=False, addrs=None, neigh=(), leases=(),
                 carrier=True):
        self.sysfs = tmp / "sys"
        self.ports = dict(ports)                     # name -> network file (None: unmanaged)
        for name in ports:
            d = self.sysfs / name
            (d / "device").mkdir(parents=True)
            (d / "type").write_text("1\n")
            (d / "carrier").write_text("1\n" if carrier else "0\n")
        for extra, kind in (("wlo1", "wireless"), ("veth0", None), ("lo", None)):  # never ports
            d = self.sysfs / extra
            d.mkdir(parents=True)
            (d / "type").write_text("772\n" if extra == "lo" else "1\n")
            if kind:
                (d / "device").mkdir()
                (d / kind).mkdir()
        self.networkd, self.nm = networkd, nm
        self.addrs = addrs if addrs is not None else [
            {"ifname": "enp2s0", "addr_info": [{"local": "192.0.2.20", "prefixlen": 24}]}]
        self.neigh, self.leases = list(neigh), list(leases)
        self.calls, self.inputs = [], []

    def run(self, args, check=False, capture_output=True, text=True, input=None):
        self.calls.append(list(args))
        if input is not None:
            self.inputs.append(input)
        out, code = "", 0
        if args[:2] == ["systemctl", "is-active"]:
            unit = args[-1]
            code = 0 if (unit == "systemd-networkd" and self.networkd) or (unit == "NetworkManager" and self.nm) else 3
        elif args[:2] == ["networkctl", "status"]:
            name = args[2]
            out = _status_text(self.ports.get(name), self.leases if self.ports.get(name) == OURS else ())
        elif args[:3] == ["ip", "-j", "-4"] and args[3] == "addr":
            out = json.dumps(self.addrs)
        elif args[:3] == ["ip", "-j", "-4"] and args[3] == "neigh":
            out = json.dumps([{"dst": a, "lladdr": "02:00:00:00:00:01", "state": ["REACHABLE"]} for a in self.neigh])
        elif args[:2] == ["networkctl", "reload"]:
            # networkd picks our file up: the port is now "configured" by it
            for n in self.ports:
                f = self._netdir / gxport.NETWORK_FILE if hasattr(self, "_netdir") else None
                if f and f.exists() and f"Name={n}\n" in f.read_text():
                    self.ports[n] = OURS
                elif self.ports[n] == OURS:
                    self.ports[n] = None
        return subprocess.CompletedProcess(args, code, out, "")


def _cfg(tmp_path, gx=None, extra=None):
    cfg = tmp_path / "config.yaml"
    raw = dict(extra or {})
    if gx is not None:
        raw["gx_port"] = gx
    cfg.write_text(json.dumps(raw))
    return cfg


def _run(tmp_path, box, gx=None, stop=None):
    status_dir, netdir = tmp_path / "status", tmp_path / "network"
    box._netdir = netdir
    seen = {}

    def once():
        if (status_dir / "status.json").exists():
            seen.update(gxport.read_status(status_dir))
            return True
        return False
    code = gxport.run(_cfg(tmp_path, gx), status_dir, netdir, box.sysfs, run=box.run, stop=stop or once,
                      notify=lambda: None)
    return code, (seen or gxport.read_status(status_dir)), netdir


# -- settings -------------------------------------------------------------------------------------

def test_settings_defaults_on_and_the_gx_address():
    s = gxport.settings_from({})
    assert (s.enabled, s.interface, s.address, s.dhcp) == (True, "", "10.43.0.1/24", True)
    assert (s.box_ip, s.gx_address, str(s.network)) == ("10.43.0.1", "10.43.0.10", "10.43.0.0/24")
    s = gxport.settings_from({"gx_port": {"enabled": "off", "address": "172.31.9.1/24", "dhcp": "no",
                                          "interface": "eth1"}})
    assert (s.enabled, s.gx_address, s.dhcp, s.interface) == (False, "172.31.9.10", False, "eth1")


@pytest.mark.parametrize("bad", [
    {"address": "8.8.8.1/24"}, {"address": "10.43.0.1/16"}, {"address": "nope"}, {"address": "169.254.1.1/24"},
    {"address": "10.43.0.10/24"},                         # the GX's own address
    {"address": "10.42.0.5/24"},                          # the box Wi-Fi's network
    {"interface": "enp1s0; reboot"}, {"enabled": "maybe"}, {"dhcp": 3},
])
def test_settings_refusals(bad):
    with pytest.raises(gxport.GxPortError):
        gxport.settings_from({"gx_port": bad})


def test_config_loads_gx_port_and_refuses_a_bad_one(tmp_path, monkeypatch):
    from cerberus import settings as settings_mod
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw-test")
    p = tmp_path / "c.yaml"
    base = {"mqtt": {"host": "192.0.2.5", "portal_id": "0123456789ab"}, "packs": {"uc100": 1},
            "storage": {"sqlite_path": str(tmp_path / "db.sqlite")}}
    p.write_text(json.dumps(base))
    assert cfg_mod.load(p).gx_port == gxport.Settings()
    p.write_text(json.dumps({**base, "gx_port": {"address": "10.42.0.1/24"}}))
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod.load(p)
    assert settings_mod.BY_KEY["gx_port.enabled"].kind == "bool"


# -- what gets written: no router, no DNS, nothing forwarded (owner decision, #53) ----------------------

def test_network_file_offers_the_gx_one_address_and_no_router_or_dns():
    text = gxport.network_file(gxport.Settings(), "enp1s0")
    for line in ("Name=enp1s0", "Address=10.43.0.1/24", "DHCPServer=yes", "PoolOffset=10", "PoolSize=1",
                 "EmitRouter=no", "EmitDNS=no", "EmitNTP=no", "LinkLocalAddressing=ipv4",
                 "ConfigureWithoutCarrier=yes", "RequiredForOnline=no", "IPv6AcceptRA=no"):
        assert line + "\n" in text
    keys = [ln.split("=")[0] for ln in text.splitlines() if "=" in ln and not ln.startswith("#")]
    for never in ("Gateway", "IPMasquerade", "IPForward", "IPv4Forwarding", "DNS", "Router", "NTP", "DHCP"):
        assert never not in keys                                    # nothing that routes or names for the GX
    assert "[Route]" not in text


def test_network_file_without_dhcp_has_no_server():
    text = gxport.network_file(gxport.Settings(dhcp=False), "enp1s0")
    assert "DHCPServer=no\n" in text and "[DHCPServer]" not in text and "Gateway" not in text


def test_rules_accept_only_dhcp_ping_and_replies_and_forward_nothing():
    rules = gxport.nft_ruleset("enp1s0")
    inp = rules.split("chain input")[1].split("chain forward")[0]
    fwd = rules.split("chain forward")[1]
    accepts = [ln.strip() for ln in inp.splitlines() if ln.strip().endswith("accept") and "policy" not in ln]
    assert accepts == ['iifname "enp1s0" ct state established,related accept',
                       'iifname "enp1s0" udp dport 67 accept',
                       'iifname "enp1s0" icmp type echo-request accept']
    assert 'iifname "enp1s0" drop' in inp
    assert 'iifname "enp1s0" drop' in fwd and 'oifname "enp1s0" drop' in fwd
    assert [ln for ln in fwd.splitlines() if "accept" in ln and "policy" not in ln] == []
    for never in ("masquerade", "snat", "dnat", "nat", "tcp dport"):
        assert never not in rules
    assert "hook forward priority -10" in rules                  # ahead of Docker's filter chains
    assert rules.startswith(f"add table inet {gxport.TABLE}\ndelete table inet {gxport.TABLE}\n")  # one swap
    assert "udp dport 67" not in gxport.nft_ruleset("enp1s0", dhcp=False)


# -- which port --------------------------------------------------------------------------------------

def test_two_port_box_takes_the_unmanaged_port(tmp_path):
    box = Box(tmp_path, {"enp1s0": None, "enp2s0": NETPLAN})
    found = gxport.ports(box.run, box.sysfs)
    assert [p["name"] for p in found] == ["enp1s0", "enp2s0"]       # not Wi-Fi, veth or loopback
    assert gxport.choose(gxport.Settings(), found) == ("enp1s0", None)


def test_one_port_box_changes_nothing_and_says_why(tmp_path):
    box = Box(tmp_path, {"enp2s0": NETPLAN})
    iface, why = gxport.choose(gxport.Settings(), gxport.ports(box.run, box.sysfs))
    assert iface is None and "one Ethernet port (enp2s0)" in why


def test_choose_refuses_ambiguity_and_ports_set_up_by_something_else():
    s = gxport.Settings()
    two_free = [{"name": "eth0", "network_file": None}, {"name": "eth1", "network_file": None}]
    assert gxport.choose(s, two_free)[0] is None and "gx_port.interface" in gxport.choose(s, two_free)[1]
    assert gxport.choose(dataclasses.replace(s, interface="eth1"), two_free) == ("eth1", None)
    lan = [{"name": "eth0", "network_file": NETPLAN}, {"name": "eth1", "network_file": None}]
    iface, why = gxport.choose(dataclasses.replace(s, interface="eth0"), lan)
    assert iface is None and "leaves it alone" in why
    assert gxport.choose(dataclasses.replace(s, interface="eth9"), lan)[0] is None
    assert gxport.choose(s, []) == (None, "no Ethernet port")
    # once set up, the port is "configured" by our own file: still ours, even beside another free port
    ours = [{"name": "eth0", "network_file": None}, {"name": "eth1", "network_file": OURS}]
    assert gxport.choose(s, ours) == ("eth1", None)


def test_parsers():
    st = _status_text(OURS, leases=("10.43.0.10",))
    assert gxport.network_file_of(st) == OURS and gxport.network_file_of(_status_text()) is None
    assert gxport.leases(st) == ["10.43.0.10"] and gxport.leases(_status_text()) == []
    neigh = json.dumps([{"dst": "10.43.0.10", "state": ["REACHABLE"]}, {"dst": "169.254.7.7", "state": ["STALE"]},
                        {"dst": "10.43.0.99", "state": ["FAILED"]}, {"dst": "fe80::1", "state": ["REACHABLE"]}])
    assert gxport.neighbors(neigh) == ["10.43.0.10", "169.254.7.7"]
    assert gxport.neighbors("not json") == []


# -- running it ----------------------------------------------------------------------------------------

def test_run_isolates_first_then_configures_the_port_and_reports(tmp_path):
    box = Box(tmp_path, {"enp1s0": None, "enp2s0": NETPLAN}, neigh=["10.43.0.10"], leases=["10.43.0.10"])
    code, st, netdir = _run(tmp_path, box)
    assert code == 0
    nft = box.calls.index(["nft", "-f", "-"])
    assert nft < box.calls.index(["networkctl", "reload"])           # never configured without its rules
    assert ["networkctl", "reconfigure", "enp1s0"] in box.calls
    assert 'iifname "enp1s0" drop' in box.inputs[0]
    text = (netdir / gxport.NETWORK_FILE).read_text()
    assert "Name=enp1s0\n" in text and "EmitRouter=no" in text
    assert (st["state"], st["interface"], st["address"], st["gx_address"], st["link"]) == \
           ("on", "enp1s0", "10.43.0.1", "10.43.0.10", "up")
    assert st["leases"] == ["10.43.0.10"] and st["neighbors"] == ["10.43.0.10"]
    assert not any(c[:2] == ["networkctl", "reload"] for c in box.calls if "enp2s0" in c)   # the LAN untouched
    assert not any("enp2s0" in c for c in box.calls if c[0] in ("ip",) and "flush" in c)


def test_a_restart_with_the_same_settings_does_not_bounce_the_port(tmp_path):
    box = Box(tmp_path, {"enp1s0": None, "enp2s0": NETPLAN})
    _run(tmp_path, box)
    box.calls.clear()
    code, st, _ = _run(tmp_path, box)
    assert code == 0 and st["state"] == "on"
    assert ["nft", "-f", "-"] in box.calls                         # the rules are always (re)applied
    assert ["networkctl", "reload"] not in box.calls                # but networkd is left alone


def test_turning_it_off_takes_the_port_down(tmp_path):
    box = Box(tmp_path, {"enp1s0": None, "enp2s0": NETPLAN})
    _, _, netdir = _run(tmp_path, box)
    box.calls.clear()
    code, st, _ = _run(tmp_path, box, gx={"enabled": False})
    assert code == 0 and st["state"] == "off"
    assert not (netdir / gxport.NETWORK_FILE).exists()
    assert ["networkctl", "reload"] in box.calls
    assert ["ip", "addr", "flush", "dev", "enp1s0"] in box.calls
    assert ["nft", "delete", "table", "inet", gxport.TABLE] in box.calls


@pytest.mark.parametrize("ports, kw, gx, state, why", [
    ({"enp2s0": NETPLAN}, {}, None, "unavailable", "one Ethernet port"),
    ({"enp1s0": None, "enp2s0": NETPLAN}, {"networkd": False}, None, "unavailable", "systemd-networkd"),
    ({"enp1s0": None, "enp2s0": NETPLAN}, {"nm": True}, None, "unavailable", "NetworkManager"),
    ({"enp1s0": None, "enp2s0": NETPLAN}, {"addrs": [{"ifname": "enp2s0", "addr_info": [
        {"local": "10.43.0.7", "prefixlen": 24}]}]}, None, "error", "already on 10.43.0.0/24"),
    ({"enp1s0": None, "enp2s0": NETPLAN}, {}, {"address": "1.2.3.4/24"}, "error", "private IPv4"),
])
def test_boxes_where_it_does_not_apply_change_nothing(tmp_path, ports, kw, gx, state, why):
    box = Box(tmp_path, ports, **kw)
    code, st, netdir = _run(tmp_path, box, gx=gx)
    assert code == 0 and st["state"] == state and why in st["error"]
    assert not (netdir / gxport.NETWORK_FILE).exists()
    assert not any(c[0] in ("nft",) or c[:2] in (["networkctl", "reload"], ["networkctl", "reconfigure"])
                   for c in box.calls)


def test_moving_to_another_port_undoes_the_old_one(tmp_path):
    box = Box(tmp_path, {"eth0": None, "eth1": None, "eth2": NETPLAN})
    _run(tmp_path, box, gx={"interface": "eth0"})
    box.calls.clear()
    _, st, netdir = _run(tmp_path, box, gx={"interface": "eth1"})
    assert st["interface"] == "eth1" and "Name=eth1\n" in (netdir / gxport.NETWORK_FILE).read_text()
    assert ["ip", "addr", "flush", "dev", "eth0"] in box.calls


def test_no_cable_is_reported(tmp_path):
    box = Box(tmp_path, {"enp1s0": None, "enp2s0": NETPLAN}, carrier=False)
    _, st, _ = _run(tmp_path, box)
    assert st["state"] == "on" and st["link"] == "down"
    assert gxport.health(st, st["updated"])["gx_seen"] is False


def test_health_and_stale():
    now = 1_800_000_000
    on = {"state": "on", "interface": "enp1s0", "address": "10.43.0.1", "gx_address": "10.43.0.10", "dhcp": True,
          "link": "up", "leases": ["10.43.0.10"], "neighbors": ["10.43.0.10"], "updated": now - 5}
    assert gxport.health(on, now) == {"state": "on", "interface": "enp1s0", "link": "up", "gx_seen": True,
                                      "devices": 1, "error": None}
    assert gxport.health({**on, "updated": now - gxport.STALE_S - 1}, now)["state"] == "stale"
    assert gxport.health({"state": "unavailable", "error": "one Ethernet port"}, now)["error"] == "one Ethernet port"
    assert gxport.health({}, now)["state"] == "unknown"


# -- the collector: which addresses, and which way --------------------------------------------------------

def _config(tmp_path, gx=None):
    p = tmp_path / "c.yaml"
    raw = {"mqtt": {"hosts": ["192.0.2.5", "venus.local"], "portal_id": "0123456789ab"}, "packs": {"uc100": 1},
           "storage": {"sqlite_path": str(tmp_path / "db.sqlite")}}
    if gx is not None:
        raw["gx_port"] = gx
    p.write_text(json.dumps(raw))
    return cfg_mod.load(p)


CABLED = {"state": "on", "gx_address": "10.43.0.10", "interface": "enp1s0", "dhcp": True, "link": "up",
          "leases": ["10.43.0.10"], "neighbors": ["10.43.0.10"]}


@pytest.fixture
def cfg_env(monkeypatch):
    monkeypatch.setenv("CERBERUS_MQTT_PASSWORD", "pw-test")
    monkeypatch.delenv("CERBERUS_MQTT_HOST", raising=False)


def test_link_down_keeps_the_configured_hosts(tmp_path, cfg_env):
    """The port is set up but no cable (ConfigureWithoutCarrier keeps its route): don't wait on it."""
    cfg = _config(tmp_path)
    for st in ({**CABLED, "link": "down"}, {**CABLED, "link": "down", "leases": [], "neighbors": []}):
        assert gxport.with_gx_port(cfg, st).mqtt.hosts == ("192.0.2.5", "venus.local")


def test_link_up_with_a_lease_puts_the_cable_first(tmp_path, cfg_env):
    cfg = _config(tmp_path)
    for st in (CABLED, {**CABLED, "neighbors": []}, {**CABLED, "leases": []}):
        assert gxport.with_gx_port(cfg, st).mqtt.hosts == ("10.43.0.10", "192.0.2.5", "venus.local")
    # carrier, but nothing has taken or answered at the GX's address (a switch, or the GX still booting)
    assert gxport.with_gx_port(cfg, {**CABLED, "leases": [], "neighbors": []}).mqtt.hosts == ("192.0.2.5", "venus.local")
    assert gxport.with_gx_port(cfg, {**CABLED, "leases": [], "neighbors": ["169.254.1.2"]}).mqtt.hosts == \
        ("192.0.2.5", "venus.local")


def test_plugging_the_gx_in_while_running_restarts_the_collector_once(tmp_path, cfg_env):
    from types import SimpleNamespace
    from cerberus.__main__ import Cerberus
    app = SimpleNamespace(cfg=_config(tmp_path), _cable_ready=False, restart_requested=False)
    plugged = lambda st: Cerberus._gx_cable_plugged_in(app, st)  # noqa: E731
    assert plugged({**CABLED, "link": "down"}) is False and not app.restart_requested
    assert plugged(CABLED) is True and app.restart_requested                     # GX on the cable: restart
    app.restart_requested = False
    assert plugged(CABLED) is False                                              # once, not every tick
    assert plugged({**CABLED, "link": "down"}) is False and not app.restart_requested   # pulled: failover
    app.cfg = gxport.with_gx_port(app.cfg, CABLED)
    assert plugged(CABLED) is False                                              # already first: no restart


def test_collector_tries_the_gx_cable_first_while_the_cable_is_in(tmp_path, cfg_env):
    cfg = _config(tmp_path)
    on = CABLED
    c = gxport.with_gx_port(cfg, on)
    assert c.mqtt.hosts == ("10.43.0.10", "192.0.2.5", "venus.local") and c.mqtt.host == "10.43.0.10"
    assert gxport.with_gx_port(c, on).mqtt.hosts == c.mqtt.hosts                     # no duplicate
    for st in ({"state": "unavailable", "error": "one Ethernet port"}, {"state": "unknown"}, {"state": "off"}):
        assert gxport.with_gx_port(cfg, st).mqtt.hosts == ("192.0.2.5", "venus.local")   # one-port box: as before
    assert gxport.with_gx_port(_config(tmp_path, {"enabled": False}), on).mqtt.hosts == ("192.0.2.5", "venus.local")


def test_via_tells_the_cable_from_the_lan():
    s = gxport.Settings()
    assert gxport.via("10.43.0.10", s) == "gx_port" and gxport.via("169.254.3.4", s) == "gx_port"
    assert gxport.via("192.0.2.5", s) == "lan" and gxport.via(None, s) is None and gxport.via("x", s) is None
    w = gxlink.LinkWatch(True, True, 180)
    w.lan_heartbeat(1000)
    assert w.view(1010, via="gx_port")["via"] == "gx_port"
    assert w.view(5000, via="gx_port")["via"] is None                                 # down: no way at all


def test_health_report_keeps_its_allowlist(tmp_path):
    """The Cloud rejects unknown fields: ``via`` stays in the box's own views."""
    from cerberus import healthreport
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO kv VALUES ('live.snapshot', ?)",
                 (json.dumps({"gx": {"lan": "ok", "lan_last": 10, "via": "gx_port"}}),))
    assert set(healthreport.gx_link(conn, 20)) == {"lan", "cause", "down_s", "fallback"}


# -- first-run discovery on either port ----------------------------------------------------------------

PORT_ON = {"state": "on", "interface": "enp1s0", "address": "10.43.0.1", "network": "10.43.0.0/24",
           "gx_address": "10.43.0.10", "dhcp": True, "link": "up", "leases": ["10.43.0.10"],
           "neighbors": ["10.43.0.10"]}


class FakeGx:
    """Which addresses answer MQTT / serve the Victron page."""

    def __init__(self, mqtt=(), web=(), gx=()):
        self.mqtt, self.web, self.gx, self.asked = set(mqtt), set(web), set(gx), []

    def port(self, h):
        self.asked.append(h)
        return h in self.mqtt

    def discover(self, status, nets=(), mdns=None):
        import ipaddress
        return firstrun.discover(networks=[ipaddress.ip_network(n) for n in nets], port_check=self.port,
                                 gx_check=lambda h: h in self.gx, resolver=lambda n: mdns,
                                 web_check=lambda h: h in self.web, gx_port=status)


def test_discovery_finds_the_gx_on_its_cable_first():
    # The same GX on its cable (10.43.0.10) and on the router's Wi-Fi (192.0.2.5); HA's broker at .2.
    gx = FakeGx(mqtt={"10.43.0.10", "192.0.2.5", "192.0.2.2"}, gx={"10.43.0.10", "192.0.2.5"})
    found = gx.discover(PORT_ON, nets=["192.0.2.0/29"], mdns="192.0.2.5")
    assert found == [
        {"host": "10.43.0.10", "address": "10.43.0.10", "mqtt": True, "via": "gx_port", "gx": True},
        {"host": "venus.local", "address": "192.0.2.5", "mqtt": True, "via": "lan", "gx": True},
        {"host": "192.0.2.2", "address": "192.0.2.2", "mqtt": True, "via": "lan", "gx": False},
    ]


def test_discovery_finds_a_link_local_gx_on_the_cable():
    st = {**PORT_ON, "leases": [], "neighbors": ["169.254.20.30"]}
    gx = FakeGx(mqtt={"169.254.20.30"}, gx={"169.254.20.30"})
    found = gx.discover(st)
    assert found == [{"host": "169.254.20.30", "address": "169.254.20.30", "mqtt": True, "via": "gx_port",
                      "gx": True}]
    assert gx.asked[:2] == ["10.43.0.10", "169.254.20.30"]          # the predictable address, then what answered


def test_discovery_on_the_lan_only_when_the_port_is_not_up():
    gx = FakeGx(mqtt={"192.0.2.5"}, gx={"192.0.2.5"})
    for st in ({"state": "unavailable", "error": "one Ethernet port"}, {"state": "unknown"}):
        found = gx.discover(st, nets=["192.0.2.0/29"])
        assert found == [{"host": "192.0.2.5", "address": "192.0.2.5", "mqtt": True, "via": "lan", "gx": True}]
        assert "10.43.0.10" not in gx.asked


def test_discovery_says_mqtt_is_off_on_the_cable_too():
    gx = FakeGx(web={"10.43.0.10"}, gx={"10.43.0.10"})
    assert gx.discover(PORT_ON) == [{"host": "10.43.0.10", "address": "10.43.0.10", "mqtt": False,
                                     "via": "gx_port", "gx": True}]


def test_discovery_does_not_scan_the_cable_subnet_twice():
    gx = FakeGx()
    gx.discover(PORT_ON, nets=["10.43.0.0/29"])
    assert gx.asked.count("10.43.0.10") == 1 and "10.43.0.3" not in gx.asked


def test_setup_on_the_cable_keeps_venus_local_as_the_wifi_fallback(monkeypatch, tmp_path):
    """Setup completes with the cable's address first, then mDNS (the GX over Wi-Fi, through the router)."""
    import yaml
    monkeypatch.delenv("CERBERUS_MQTT_PASSWORD", raising=False)
    cfgp = tmp_path / "config.yaml"
    firstrun.complete(cfgp, "10.43.0.10", "10.43.0.10", "pw-1234", "0123456789ab", {"packs": {"uc100": 1}},
                      str(tmp_path / "db.sqlite"))
    assert yaml.safe_load(cfgp.read_text())["mqtt"]["hosts"] == ["10.43.0.10", "venus.local"]


def test_health_endpoint_shows_the_cable_and_which_way(tmp_path):
    import threading
    import urllib.request
    from cerberus.db import Db
    from cerberus.ui.server import App, serve
    db = Db(str(tmp_path / "c.db"))
    db.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('live.snapshot', ?)",
                    (json.dumps({"gx": {"lan": "ok", "via": "gx_port"}}),))
    db.conn.commit()
    st = tmp_path / "gxs"
    import time
    gxport.write_status({**PORT_ON, "updated": int(time.time())}, st)
    app = App(str(tmp_path / "c.db"), 1, 310, None)
    app.gx_port_status_dir = st
    app.wifi_status_dir = tmp_path / "nowifi"
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/health", timeout=5) as r:
            h = json.loads(r.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert h["gx_port"] == {"state": "on", "interface": "enp1s0", "link": "up", "gx_seen": True, "devices": 1,
                            "error": None, "via": "gx_port"}
