"""Victron's VRM cloud MQTT relay, as a last-resort feed.

Why this exists
---------------
The collector reads the Cerbo over the LAN, so it is blind whenever the two
are on different networks. On 2026-10-20 the van drove off and came back with
the Cerbo on its Starlink while this Mac sat on the house WiFi: the feed was
dead for hours although the Cerbo was online the whole time and VRM showed it
live. VRM relays the same ``N/<portal>/...`` tree over the internet, so the
collector can keep recording from anywhere either side has a connection.

It is a fallback, not a host in the failover ring: it needs TLS, a different
login (VRM account email + ``Token <VRM_TOKEN>``), and its ACL admits only the
installation's own topics, so Cerberus's availability/state/discovery topics —
which ride the local broker to Home Assistant — cannot go there. It therefore
runs as a second, read-only CerboMqtt, started after the local link has been
down a while and stopped as soon as it is back. Every relayed message crosses
the Cerbo's uplink, so it should not run while the LAN works.

Verified 2026-10-20: mqtt52.victronenergy.com, per-pack battery, vebus Dc/0,
alarms and solar all arrive.
"""

from __future__ import annotations

import dataclasses
import json
import ssl
import urllib.request
from pathlib import Path

from .config import MqttConfig

PORT = 8883
API = "https://vrmapi.victronenergy.com/v2"
# Victron's own CA (github.com/victronenergy/dbus-mqtt, venus-ca.crt). The
# relay's certificate does not chain to a public root.
CA_FILE = Path(__file__).with_name("venus-ca.crt")


def broker_host(portal_id: str) -> str:
    """Victron shards installations over mqtt0..127 by the portal id's bytes."""
    return f"mqtt{sum(ord(c) for c in portal_id) % 128}.victronenergy.com"


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(CA_FILE))
    # The CA dates from 2014 and has no keyUsage extension, which Python 3.13+
    # rejects under VERIFY_X509_STRICT. Chain and hostname are still verified.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def account_email(token: str, timeout: float = 10.0) -> str:
    """The relay's username is the VRM account email, not anything in the token."""
    req = urllib.request.Request(f"{API}/users/me",
                                 headers={"x-authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["user"]["email"]


def cloud_config(local: MqttConfig, token: str, email: str) -> MqttConfig:
    host = broker_host(local.portal_id)
    return dataclasses.replace(local, host=host, hosts=(host,), port=PORT,
                               username=email, password=f"Token {token}")
