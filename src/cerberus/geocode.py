"""Home position from a ZIP code.

Owners know their ZIP code, not their latitude. The Settings screen takes a
US ZIP and turns it into the home latitude/longitude that weather already
uses (weather.latitude / weather.longitude), so nothing downstream changes.

The lookup is one HTTPS request to zippopotam.us (free, no key) when the ZIP
is saved, never on a schedule. A ZIP's centroid is within a few miles of any
address in it, which is far finer than a weather forecast's grid.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

ZIP_URL = "https://api.zippopotam.us/us/{zip}"
TIMEOUT_S = 8
_ZIP = re.compile(r"^(\d{5})(?:-\d{4})?$")


class GeocodeError(ValueError):
    pass


def normalize_zip(value) -> str:
    """'07960' or '07960-1234' -> '07960'; anything else is an error."""
    m = _ZIP.match(str(value or "").strip())
    if not m:
        raise GeocodeError(f"{value!r} is not a US ZIP code (5 digits)")
    return m.group(1)


def _fetch(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Cerberus (van telemetry; self-hosted)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise GeocodeError(f"ZIP lookup failed (HTTP {err.code}); try again later") from None
    except (OSError, ValueError):
        raise GeocodeError("couldn't reach the ZIP lookup service; check the internet "
                           "connection, or enter latitude and longitude instead") from None


def lookup_zip(zip_code, fetch=_fetch) -> dict:
    """{zip, latitude, longitude, place} for a US ZIP code."""
    z = normalize_zip(zip_code)
    data = fetch(ZIP_URL.format(zip=z))
    places = (data or {}).get("places") or []
    if not places:
        raise GeocodeError(f"ZIP code {z} wasn't found")
    p = places[0]
    try:
        lat, lon = round(float(p["latitude"]), 4), round(float(p["longitude"]), 4)
    except (KeyError, TypeError, ValueError):
        raise GeocodeError(f"ZIP lookup returned no position for {z}") from None
    place = ", ".join(x for x in (p.get("place name"), p.get("state abbreviation")) if x)
    return {"zip": z, "latitude": lat, "longitude": lon, "place": place}
