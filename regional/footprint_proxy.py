"""
Node-to-building footprint proxy (Phase RC-3c, 10 Aug).

RC-3b measured footprint for every OSM company mapped as a building POLYGON
(a `way`) — 342 of them. That left 629 companies mapped as a bare point
(`node`) with no shape to measure at all, plus 7 multipolygon `relation`s.
A node genuinely has no area; the register's own triage correctly leaves
those in the low-prior tier (footprint_m2 stays NULL, never read as "small").

This module closes part of that gap safely: a business is very often mapped
as BOTH a point (carrying the identifying office=company/man_made=works tag
our discovery query looks for) AND a separate nearby building outline that
carries no such tag and was therefore invisible to discovery. Confirmed live
10 Aug: "Wilhelm Fischer Spezialmaschinenfabrik GmbH" (a node) has its own
building polygon 25m away, independently tagged with the SAME company name.

THE SAFETY RULE IS THE WHOLE POINT. Nearest-building-to-a-point is exactly
the kind of proxy that goes quietly wrong in a dense area — a small company
whose address happens to sit inside or beside a large shared building would
be assigned that building's footprint and look like a major employer for no
real reason. So a match is only ever accepted when the evidence is
unambiguous:

  1. NAME MATCH (high confidence) — a candidate building carries its own
     `name` tag and it is the same company (regional.matcher.is_same,
     the existing project-wide company-identity rule). The Fischer case
     above is exactly this.
  2. UNAMBIGUOUS NEAREST (lower confidence, still safe) — no building in the
     search radius has a matching name, but exactly ONE candidate exists
     within a materially tighter radius, with nothing else competing for it.

  Anything else — multiple untagged candidates, or nothing nearby at all —
  is left unresolved. That company simply stays in tier 3, which is what
  "put the rest in tier 3" means here: this module NEVER promotes on a guess.

The match method and distance are recorded in `raw` for every accepted match
so a human can audit exactly why a given footprint was assigned.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from regional.discovery import OVERPASS_MIRRORS, _USER_AGENT
from regional.geocode import haversine_km
from regional.geometry import polygon_area_m2, polygon_centroid
from regional.matcher import is_same

logger = logging.getLogger(__name__)

# Search radius for candidate buildings around a company's point.
SEARCH_RADIUS_M = 40.0
# If nothing matches by name, a single candidate within this tighter radius
# (and nothing else in the wider SEARCH_RADIUS_M) is accepted as unambiguous.
UNAMBIGUOUS_RADIUS_M = 15.0


@dataclass
class BuildingCandidate:
    osm_id: int
    tags: dict
    area_m2: float
    centroid: Tuple[float, float]


@dataclass
class Match:
    area_m2: float
    method: str          # "name_match" | "unambiguous_nearest"
    distance_m: float
    osm_id: int
    matched_name: Optional[str] = None


def _distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return haversine_km(a, b) * 1000.0


def match_one(company_name: str, company_point: Tuple[float, float],
             candidates: List[BuildingCandidate]) -> Optional[Match]:
    """
    Pure decision function — no network, fully unit-testable. Given one
    company and the buildings already fetched near it, decide whether a safe
    match exists. Returns None rather than guess.
    """
    in_range = []
    for c in candidates:
        d = _distance_m(company_point, c.centroid)
        if d <= SEARCH_RADIUS_M:
            in_range.append((d, c))
    if not in_range:
        return None

    # Rule 1: name match, regardless of distance within the search radius —
    # a real name match is stronger evidence than proximity alone.
    for d, c in in_range:
        name = c.tags.get("name")
        if name and is_same(company_name, name):
            return Match(area_m2=c.area_m2, method="name_match",
                        distance_m=round(d, 1), osm_id=c.osm_id, matched_name=name)

    # Rule 2: unambiguous nearest — exactly one candidate sits within the
    # tighter radius, and nothing else (even outside that tighter radius but
    # still in range) is competing for it within the wider search radius.
    close = [(d, c) for d, c in in_range if d <= UNAMBIGUOUS_RADIUS_M]
    if len(close) == 1 and len(in_range) == 1:
        d, c = close[0]
        return Match(area_m2=c.area_m2, method="unambiguous_nearest",
                    distance_m=round(d, 1), osm_id=c.osm_id)

    return None  # ambiguous — leave unresolved, stays tier 3


def _fetch_buildings_near(points: List[Tuple[float, float]],
                          radius_m: float = SEARCH_RADIUS_M) -> Dict[int, BuildingCandidate]:
    """
    One Overpass call for a whole batch of points. Overpass returns the SET
    UNION of buildings near ANY point — it does not tell us which point
    pulled in which building — so per-company matching happens afterwards in
    match_one() using each company's own stored coordinates. That is correct
    regardless of which point's `around` clause originally found a given
    building, since match_one() only cares about actual distance.
    """
    clauses = "".join(f'way(around:{radius_m},{lat},{lon})["building"];'
                      for lat, lon in points)
    query = f"[out:json][timeout:180];({clauses});out geom;"
    body = urllib.parse.urlencode({"data": query}).encode()

    for mirror in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(mirror, data=body,
                                         headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as r:
                payload = json.load(r)
            if payload.get("elements") is not None:
                break
        except Exception as exc:
            logger.warning(f"[FootprintProxy] mirror {mirror} failed: {exc}")
    else:
        return {}

    out: Dict[int, BuildingCandidate] = {}
    for el in payload.get("elements", []):
        geom = el.get("geometry")
        if not geom:
            continue
        out[el["id"]] = BuildingCandidate(
            osm_id=el["id"], tags=el.get("tags") or {},
            area_m2=round(polygon_area_m2(geom), 1),
            centroid=polygon_centroid(geom),
        )
    return out


def match_batch(companies: list, *, radius_m: float = SEARCH_RADIUS_M) -> Dict[str, Match]:
    """
    companies: objects with .id (str-able), .name, .lat, .lon (all required —
    callers must pre-filter to rows with known coordinates).

    Returns {company_id: Match} for only the companies where a safe match was
    found. Everything else is implicitly "leave alone" — the caller does not
    need to do anything for a company absent from the result.
    """
    points = [(c.lat, c.lon) for c in companies]
    buildings = list(_fetch_buildings_near(points, radius_m).values())

    results: Dict[str, Match] = {}
    for c in companies:
        m = match_one(c.name, (c.lat, c.lon), buildings)
        if m:
            results[str(c.id)] = m
    return results
