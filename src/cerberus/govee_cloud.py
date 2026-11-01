"""Cabin H5179 readings via the Govee cloud API — the fallback for BLE range.

Why this exists
---------------
cerberus.govee reads the cabin sensor from its BLE advertisements, which only
works while this machine is within radio range of the van. Parked at home on
2026-10-04 the sidecar caught roughly 1 in 7 adverts at -90 to -99 dBm, and
none at all once the laptop moves further away. The H5179 also has WiFi, and
whenever it can reach a network it uploads to Govee's cloud — so the cloud has
the cabin temperature exactly when BLE does not.

Nothing local can read it over WiFi: the Govee LAN API covers lights only (a
discovery scan on 239.255.255.250:4001 got no reply), and the sensor exposes
no mDNS service. The cloud API is the only WiFi path.

What the API does and does not say
----------------------------------
``POST /router/api/v1/device/state`` returns ``online``, ``sensorTemperature``
and ``sensorHumidity`` — and **no timestamp**. The value is whatever the sensor
last uploaded, so a stored row means "the cloud said this at ts", not "the air
was this at ts". Rows are tagged ``source='govee_cloud'`` so BLE rows (true
60 s samples) stay distinguishable.

Temperature arrives in **Fahrenheit**: 64.22 from the cloud matched 64.2 F from
the BLE decode on 2026-10-04, which matches the project convention of ambient
in F. Battery and RSSI are not in the response.

The API key comes from the ``GOVEE_API_KEY`` environment variable, loaded from
.env by bin/run-collector.sh — never config.yaml, which is in the repo.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

STATE_ENDPOINT = "https://openapi.api.govee.com/router/api/v1/device/state"


@dataclass(frozen=True)
class CloudReading:
    air_temp_f: float | None
    humidity_pct: float | None


def parse_state(payload: dict) -> CloudReading | None:
    """Pull temperature and humidity out of a device/state response.

    None when the API reports an error or the device as offline: an offline
    sensor's last value can be hours old, and storing it as a fresh reading
    would be worse than a gap.
    """
    if payload.get("code") != 200:
        log.warning("govee cloud error: %s %s", payload.get("code"), payload.get("msg"))
        return None
    caps = {
        c.get("instance"): (c.get("state") or {}).get("value")
        for c in (payload.get("payload") or {}).get("capabilities") or []
    }
    if caps.get("online") is not True:
        log.info("govee cloud: sensor offline, skipping")
        return None
    temp, rh = caps.get("sensorTemperature"), caps.get("sensorHumidity")
    if temp is None and rh is None:
        return None
    return CloudReading(
        air_temp_f=None if temp is None else round(float(temp), 2),
        humidity_pct=None if rh is None else round(float(rh), 1),
    )


def fetch(api_key: str, sku: str, device: str, timeout: int = 10) -> CloudReading | None:
    """Current reading from the cloud, or None if anything at all goes wrong.

    Returns None rather than raising, for the same reason as weather.fetch: a
    missing cabin row must never interrupt electrical collection.
    """
    body = json.dumps({
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": sku, "device": device},
    }).encode()
    req = urllib.request.Request(
        STATE_ENDPOINT,
        data=body,
        headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning("govee cloud fetch failed: %s", exc)
        return None
    try:
        return parse_state(payload)
    except (AttributeError, TypeError, ValueError) as exc:
        log.warning("govee cloud payload unexpected shape: %s", exc)
        return None
