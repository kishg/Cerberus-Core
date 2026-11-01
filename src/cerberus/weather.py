"""Outdoor conditions from met.no, stored alongside the electrical samples.

Why this exists
---------------
The van's energy balance has two seasonal terms that move in opposite
directions, and only one of them was ever measured:

* **Solar falls** with day length and sun angle — visible in ``solar_samples``.
* **Load also falls**, because the fridge's compressor duty scales with the
  delta-T it pumps against and the MaxxFan runs less when it is cool.

Without an ambient reading the second term can only be argued from physics.
On 2026-09-19 that gap was concrete: daily DC load across 34 days correlated
with *pack* temperature at **r = +0.016** — no signal at all. The packs sit in
a cabinet with thermal mass and self-heat from charge current, and have never
been recorded below 20.7 C. They are not an ambient proxy and never will be.

Home Assistant already pulls met.no (``weather.forecast_home``) and it carries
air temperature, cloud cover and humidity. But HA's recorder keeps states for
``purge_keep_days`` — 10 by default, which is what this install runs — and
weather attributes generate no long-term statistics, so that history is
continuously deleted. Ten days is far too short to see a season.

So Cerberus fetches met.no itself. It already owns a time-series database with
no retention limit, and putting ambient in the *same* file as load and yield is
what makes the correlation a single join rather than a cross-database chore.
Fetching upstream rather than reading HA also means this keeps working when HA
is down, and needs no token, no automation and no MQTT round trip.

Units
-----
met.no publishes Celsius. This module converts to **Fahrenheit** and the column
is named ``air_temp_f`` to say so, because the project's convention is ambient
in F and equipment in C — and the way that convention gets violated is a bare
``temperature`` column whose unit lives only in someone's memory.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


@dataclass(frozen=True)
class WeatherSample:
    air_temp_f: float | None
    cloud_pct: float | None
    humidity_pct: float | None
    wind_mph: float | None


def _f(celsius: float | None) -> float | None:
    return None if celsius is None else round(celsius * 9.0 / 5.0 + 32.0, 1)


def fetch(lat: float, lon: float, user_agent: str, timeout: int = 10) -> WeatherSample | None:
    """One observation for *now*, or None if anything at all goes wrong.

    met.no rejects requests without an identifying User-Agent, so `user_agent`
    is required rather than defaulted — their terms ask for a real contact and
    a shared default would be both rude and fragile.

    Returns None rather than raising: a missing weather sample must never be
    able to interrupt electrical collection, which is the job that matters.
    """
    url = f"{ENDPOINT}?lat={lat:.4f}&lon={lon:.4f}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.warning("weather fetch failed: %s", exc)
        return None

    try:
        series = payload["properties"]["timeseries"]
        if not series:
            return None
        # timeseries[0] is the nearest hour to now; "instant" is the observed
        # state rather than a forecast interval, which is what we want to pair
        # with a 30 s electrical sample.
        d = series[0]["data"]["instant"]["details"]
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("weather payload unexpected shape: %s", exc)
        return None

    wind_ms = d.get("wind_speed")
    return WeatherSample(
        air_temp_f=_f(d.get("air_temperature")),
        cloud_pct=d.get("cloud_area_fraction"),
        humidity_pct=d.get("relative_humidity"),
        wind_mph=None if wind_ms is None else round(wind_ms * 2.23694, 1),
    )
