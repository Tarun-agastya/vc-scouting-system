"""
Geocoding + distance for the regional register (Phase RC-1).

Uses Nominatim (OpenStreetMap's geocoder): free, no API key, no account, and
explicitly permitted for this kind of low-volume lookup provided the usage
policy is respected — a real User-Agent and at most one request per second.
Both are enforced here rather than left to the caller.

Deliberately NOT adding `geopy` as a dependency for this. The project runs
unattended on a Mac mini and keeps its dependency surface small on purpose
(see ui/static/js/charts.js's header for the same reasoning applied to
charting). This is ~60 lines against a stable, documented HTTP endpoint, and
the results are cached on disk so a re-import re-geocodes nothing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from math import asin, cos, radians, sin, sqrt
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Memmingen town centre — the radius origin for the whole register.
MEMMINGEN = (47.9878, 10.1815)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_PROJECT_ROOT, "regional", "_geocode_cache.json")

# Nominatim's usage policy requires an identifying User-Agent and <= 1 req/s.
_USER_AGENT = "GreenTechHub-RegionalRegister/1.0 (+https://gt-hub.de; internal use)"
_MIN_INTERVAL_S = 1.1

_lock = threading.Lock()
_last_request_at = 0.0
_cache: Optional[dict] = None


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km. Straight-line, not driving distance —
    correct for a 'within X km' catchment definition, and it never depends on
    a routing service being up."""
    lat1, lon1, lat2, lon2 = map(radians, [a[0], a[1], b[0], b[1]])
    h = (sin((lat2 - lat1) / 2) ** 2
         + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * asin(sqrt(h))


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_PATH) as fh:
                _cache = json.load(fh)
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as fh:
            json.dump(_cache or {}, fh, indent=2, ensure_ascii=False)
    except Exception as exc:  # cache is an optimisation, never load-bearing
        logger.debug(f"[Regional] could not persist geocode cache: {exc}")


def _throttle() -> None:
    global _last_request_at
    with _lock:
        wait = _MIN_INTERVAL_S - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def geocode(place: str, *, country: str = "Germany") -> Optional[dict]:
    """
    Resolve a free-text location to coordinates. Returns
    {lat, lon, display_name, postcode} or None — never raises, since a single
    unresolvable "Standort" must not abort a whole import.

    Cached on disk by (place, country): the sheet has many companies sharing
    one town, and a re-import should cost zero requests.
    """
    key = f"{place.strip().lower()}|{country.lower()}"
    cache = _load_cache()
    if key in cache:
        return cache[key]

    params = urllib.parse.urlencode({
        "q": f"{place}, {country}",
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": _USER_AGENT},
    )

    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except Exception as exc:
        logger.warning(f"[Regional] geocode failed for {place!r}: {exc}")
        return None

    if not data:
        logger.warning(f"[Regional] no geocode result for {place!r}")
        cache[key] = None
        _save_cache()
        return None

    hit = data[0]
    result = {
        "lat": float(hit["lat"]),
        "lon": float(hit["lon"]),
        "display_name": hit.get("display_name", ""),
        "postcode": (hit.get("address") or {}).get("postcode"),
    }
    cache[key] = result
    _save_cache()
    return result


def locate(place: str, *, country: str = "Germany") -> dict:
    """
    geocode() plus distance from Memmingen. Always returns a dict; when the
    lookup fails, coordinates and distance are None and `in_radius` is False
    so an unresolvable row is excluded from the catchment rather than silently
    assumed to be inside it.
    """
    hit = geocode(place, country=country)
    if not hit:
        return {"lat": None, "lon": None, "distance_km": None,
                "in_radius": False, "postcode": None, "display_name": None}

    dist = haversine_km(MEMMINGEN, (hit["lat"], hit["lon"]))
    return {
        "lat": hit["lat"],
        "lon": hit["lon"],
        "distance_km": round(dist, 2),
        "in_radius": dist <= 50.0,
        "postcode": hit.get("postcode"),
        "display_name": hit.get("display_name"),
    }
