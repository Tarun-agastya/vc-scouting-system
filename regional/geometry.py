"""
Pure geometry helpers for OSM-derived size proxies (Phase RC-3b/c).

No network, no database — kept separate so both scripts/rc_footprints.py
(building polygons) and regional/footprint_proxy.py (node-to-building
matching) share one implementation, and so both can be unit tested without
touching Overpass.
"""
from __future__ import annotations

import math
from typing import List, Tuple

Coord = dict  # {"lat": float, "lon": float}, OSM's own geometry point shape


def polygon_area_m2(coords: List[Coord]) -> float:
    """Shoelace on a local equirectangular projection — accurate to well
    within a percent for building-sized polygons at this latitude."""
    if len(coords) < 3:
        return 0.0
    lat0 = sum(c["lat"] for c in coords) / len(coords)
    mx = 111_320.0 * math.cos(math.radians(lat0))
    my = 110_574.0
    pts = [(c["lon"] * mx, c["lat"] * my) for c in coords]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_centroid(coords: List[Coord]) -> Tuple[float, float]:
    """Vertex-average centroid — not area-weighted, but accurate enough at
    building scale to compare against a nearby point for matching purposes."""
    if not coords:
        return (0.0, 0.0)
    return (sum(c["lat"] for c in coords) / len(coords),
            sum(c["lon"] for c in coords) / len(coords))
