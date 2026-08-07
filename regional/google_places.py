"""
Google Places discovery for the regional register (Phase RC-3, Lever D).

WHY THIS SOURCE, given three free ones already exist. The free sources reach
53% recall and each misses for a structural reason: Wikidata and Wikipedia key
on headquarters and notability, so they lose plants of foreign parents and
family Mittelstand entirely; OSM is site-based and catches those, but only
where a volunteer has mapped the site. Google is the one source where the
companies list *themselves* — a Mittelstand firm maintains a Business Profile
because it wants to be found, and that is exactly the population the other
three structurally cannot see.

Crucially it also has `manufacturer` and `wholesaler` place types, which makes
it targetable at industry rather than at consumer storefronts. That was the
main doubt about this source, and it turned out to be unfounded.

TWO REAL CONSTRAINTS, both handled here:

  1. Nearby Search returns at most 20 results per call. A single 50 km query
     would therefore return 20 companies and silently imply that is all of
     them. This module tiles the circle into small overlapping discs and
     subdivides any tile that comes back full, so "full" is never mistaken for
     "complete".

  2. It costs money — unlike everything else in this project. Pro-tier fields
     are $32 per 1000 calls, so a full sweep is roughly $20-30. Cost control
     is therefore not optional: `estimate_sweep()` computes the tile count and
     projected spend WITHOUT calling the API, every run takes a hard call cap,
     and the field mask is pinned to Pro-tier fields so a stray extra field
     cannot silently promote the whole sweep to the more expensive tier.

Returns names, addresses and coordinates. It does NOT return employee counts —
nothing does — so its output still flows into the same RC-4 enrichment queue.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from regional.discovery import Candidate
from regional.filters import clean_name
from regional.geocode import MEMMINGEN, haversine_km

logger = logging.getLogger(__name__)

ENDPOINT = "https://places.googleapis.com/v1/places:searchNearby"

# Pro-tier fields only. Adding websiteUri or nationalPhoneNumber would push
# every call into the Enterprise tier and change the cost of the whole sweep,
# so the mask is deliberately fixed here rather than passed in by callers.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
])

# Google caps a Nearby Search response at 20 places.
MAX_PER_CALL = 20

# USD per 1000 calls at the Pro tier (Feb 2026 pricing). Used only to show an
# estimate before spending anything.
USD_PER_1000 = 32.0

# B2B / industrial types. Deliberately excludes consumer categories
# (restaurant, hairdresser, shop...) which would otherwise dominate every tile
# and burn the call budget on places that can never be membership prospects.
B2B_TYPES = [
    "manufacturer",
    "wholesaler",
    "storage",
    "general_contractor",
    "corporate_office",
    "moving_company",
    "farm",
    "electrician",
    "plumber",
    "car_dealer",
]


def _tiles(center=MEMMINGEN, radius_km: float = 50.0,
           tile_km: float = 4.0) -> List[Tuple[float, float]]:
    """
    Cover a circle with a grid of tile centres, keeping only those inside it.

    Overlap is intentional: a company sitting exactly on a tile boundary would
    otherwise be at the mercy of rounding. The grid step is tile_km rather than
    2*tile_km, so neighbouring discs overlap by half a radius.
    """
    lat0, lon0 = center
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(lat0))

    out: List[Tuple[float, float]] = []
    steps = int(math.ceil(radius_km / tile_km))
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            lat = lat0 + (i * tile_km) / km_per_deg_lat
            lon = lon0 + (j * tile_km) / km_per_deg_lon
            # Keep a tile whose centre is within the radius plus its own reach,
            # so companies near the rim are not clipped.
            if haversine_km(center, (lat, lon)) <= radius_km + tile_km:
                out.append((lat, lon))
    return out


def estimate_sweep(radius_km: float = 50.0, tile_km: float = 4.0,
                   types: Optional[List[str]] = None) -> dict:
    """
    What a full sweep would cost, computed WITHOUT calling the API.

    Always run this before spending. One call per tile per type group; the
    figure is a floor, since tiles that come back full get subdivided.
    """
    tiles = _tiles(radius_km=radius_km, tile_km=tile_km)
    groups = 1 if types is None else max(1, math.ceil(len(types) / 10))
    calls = len(tiles) * groups
    return {
        "tiles": len(tiles),
        "type_groups": groups,
        "min_calls": calls,
        "estimated_usd": round(calls / 1000 * USD_PER_1000, 2),
        "note": "floor, not a ceiling — full tiles are subdivided and cost more",
    }


def _search_tile(api_key: str, lat: float, lon: float, radius_m: float,
                 types: List[str], language: str = "de") -> Tuple[List[dict], bool]:
    """One Nearby Search. Returns (places, hit_the_cap)."""
    body = json.dumps({
        "includedTypes": types,
        "maxResultCount": MAX_PER_CALL,
        "languageCode": language,
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon},
                       "radius": radius_m}
        },
    }).encode()

    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        # 403/429 mean the key or the quota is the problem, not this tile —
        # raise so the caller stops rather than burning through every tile
        # collecting the same error.
        if exc.code in (401, 403, 429):
            raise RuntimeError(f"Google Places refused the request "
                               f"(HTTP {exc.code}): {detail}") from exc
        logger.warning(f"[GooglePlaces] tile ({lat:.3f},{lon:.3f}) HTTP {exc.code}: {detail}")
        return [], False
    except Exception as exc:
        logger.warning(f"[GooglePlaces] tile ({lat:.3f},{lon:.3f}) failed: {exc}")
        return [], False

    places = payload.get("places", []) or []
    return places, len(places) >= MAX_PER_CALL


def discover(api_key: str, *, radius_km: float = 50.0, tile_km: float = 4.0,
             max_calls: int = 200, types: Optional[List[str]] = None,
             center=MEMMINGEN) -> Tuple[List[Candidate], dict]:
    """
    Sweep the radius and return (candidates, stats).

    `max_calls` is a hard stop, not a target: this is the only paid source in
    the project, and the batch cap is the only cost control that exists
    (ingestion/web_search.py set the same precedent for Tavily). The sweep
    stops cleanly when it is reached and reports how much was left uncovered,
    rather than pretending it finished.
    """
    types = types or B2B_TYPES
    tiles = _tiles(center=center, radius_km=radius_km, tile_km=tile_km)
    stats = {"tiles_total": len(tiles), "tiles_done": 0, "calls": 0,
             "subdivided": 0, "raw_places": 0, "budget_exhausted": False}

    seen: dict = {}
    # (lat, lon, radius_m) work queue, so a full tile can push finer children.
    queue = [(lat, lon, tile_km * 1000) for lat, lon in tiles]

    while queue:
        if stats["calls"] >= max_calls:
            stats["budget_exhausted"] = True
            logger.warning(f"[GooglePlaces] call cap {max_calls} reached with "
                           f"{len(queue)} tiles unvisited — results are partial")
            break

        lat, lon, radius_m = queue.pop(0)
        places, full = _search_tile(api_key, lat, lon, radius_m, types)
        stats["calls"] += 1
        stats["tiles_done"] += 1
        stats["raw_places"] += len(places)

        for p in places:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            name = clean_name((p.get("displayName") or {}).get("text", ""))
            if not name:
                continue
            loc = p.get("location") or {}
            plat, plon = loc.get("latitude"), loc.get("longitude")
            if plat is not None and haversine_km(center, (plat, plon)) > radius_km:
                continue          # rim tiles reach outside the catchment
            seen[pid] = Candidate(
                name=name,
                source="google_places",
                city=None,        # parsed from formattedAddress below
                lat=plat, lon=plon,
                branche=p.get("primaryType"),
                source_url=f"https://www.google.com/maps/place/?q=place_id:{pid}",
                raw={"address": p.get("formattedAddress"),
                     "types": p.get("types"), "place_id": pid},
            )

        # A full response means the tile is truncated — there may be more here
        # than Google will return at this radius. Split it rather than accept
        # a silently capped answer.
        if full and radius_m > 800:
            stats["subdivided"] += 1
            half = radius_m / 2
            off = half / 111_320.0
            for dlat, dlon in ((off, off), (off, -off), (-off, off), (-off, -off)):
                queue.append((lat + dlat, lon + dlon, half))

    # Pull the town out of "Street 1, 87700 Memmingen, Germany".
    for cand in seen.values():
        addr = (cand.raw or {}).get("address") or ""
        parts = [p.strip() for p in addr.split(",")]
        for part in parts:
            bits = part.split()
            if len(bits) >= 2 and bits[0].isdigit() and len(bits[0]) == 5:
                cand.city = " ".join(bits[1:])
                cand.raw["postcode"] = bits[0]
                break

    stats["unique_companies"] = len(seen)
    stats["estimated_usd"] = round(stats["calls"] / 1000 * USD_PER_1000, 2)
    return list(seen.values()), stats
