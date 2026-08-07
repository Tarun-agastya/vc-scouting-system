"""
Free-source company discovery for the regional register (Phase RC-2).

Three sources, chosen because they fail in *different* directions — measured
6 Aug against the 49 known in-scope companies:

    Wikidata SPARQL   36.7%   structured, HQ-keyed
    Wikipedia cats    20.4%   curated, notability-keyed
    OSM / Overpass    28.6%   SITE-keyed
    union             59.2%

The union beats every source alone because of that complementarity. OSM is
what recovers plants of foreign parents (3M Kempten, HAWE) that HQ-keyed
Wikidata structurally cannot see, since it maps a factory where it physically
stands regardless of who owns it.

A note on the notability bias: for this particular problem it is a FEATURE.
The target band is 100-4000 employees, and "has an employee count on
Wikidata" / "is mapped as an industrial site" both correlate with being large
enough to matter. A complete registry dump (OffeneRegister) would enumerate
every one-person UG in the radius with no size signal at all to filter on —
see REGIONAL_SME_PLAN.md §3 Lever E.

All three are free, keyless, and used within their published usage policies.
Nothing here scrapes a commercial platform.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from regional.filters import accept, clean_name
from regional.geocode import MEMMINGEN, haversine_km

logger = logging.getLogger(__name__)

_USER_AGENT = "GreenTechHub-RegionalRegister/1.0 (+https://gt-hub.de; internal use)"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://de.wikipedia.org/w/api.php"

# Overpass mirrors, tried in order. A 50 km multi-clause query is heavy enough
# that any single instance returns 504 intermittently — observed on both
# overpass-api.de (6 Aug) and kumi.systems (7 Aug) for the SAME query that had
# succeeded a day earlier. One flaky mirror must not silently cost the whole
# OSM source, which is the only site-based one and the only one that finds
# plants of foreign parents.
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# Landkreise the 50 km circle touches — it crosses from Bavaria into
# Baden-Württemberg, which is exactly the direction the manual sheet
# under-covers.
WIKIPEDIA_CATEGORIES = [
    "Unternehmen (Memmingen)",
    "Unternehmen (Landkreis Unterallgäu)",
    "Unternehmen (Landkreis Oberallgäu)",
    "Unternehmen (Kempten (Allgäu))",
    "Unternehmen (Landkreis Ostallgäu)",
    "Unternehmen (Landkreis Neu-Ulm)",
    "Unternehmen (Landkreis Günzburg)",
    "Unternehmen (Landkreis Biberach)",
    "Unternehmen (Landkreis Ravensburg)",
    "Unternehmen (Alb-Donau-Kreis)",
    "Unternehmen (Ulm)",
    "Produzierendes Unternehmen (Landkreis Unterallgäu)",
    "Produzierendes Unternehmen (Landkreis Oberallgäu)",
    "Produzierendes Unternehmen (Landkreis Biberach)",
    "Produzierendes Unternehmen (Landkreis Ravensburg)",
]


@dataclass
class Candidate:
    """One discovered company, before dedupe and before enrichment."""
    name: str
    source: str
    city: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    employees: Optional[int] = None
    branche: Optional[str] = None
    website: Optional[str] = None
    source_url: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def distance_km(self) -> Optional[float]:
        if self.lat is None or self.lon is None:
            return None
        return round(haversine_km(MEMMINGEN, (self.lat, self.lon)), 2)


def _get_json(url: str, *, data: Optional[bytes] = None, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ── Wikidata ────────────────────────────────────────────────────────────────

_WIKIDATA_QUERY = """
SELECT DISTINCT ?company ?companyLabel ?employees ?placeLabel ?industryLabel
                ?website ?coord
WHERE {
  SERVICE wikibase:around {
    ?place wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point(%(lon)s %(lat)s)"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "%(radius)s" .
  }
  ?company wdt:P159 ?place .
  ?company wdt:P31/wdt:P279* wd:Q4830453 .
  # Exclude dissolved companies. Without this the sweep returns Schlecker
  # (36,000 employees, insolvent since 2012) as a live prospect — Wikidata
  # keeps historical companies, and an employee count with no liveness check
  # is worse than no employee count, because it looks authoritative.
  FILTER NOT EXISTS { ?company wdt:P576 ?dissolved . }
  OPTIONAL { ?company wdt:P1128 ?employees . }
  OPTIONAL { ?company wdt:P452  ?industry . }
  OPTIONAL { ?company wdt:P856  ?website . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de,en". }
}
"""


def from_wikidata(radius_km: int = 50, center=MEMMINGEN) -> List[Candidate]:
    q = _WIKIDATA_QUERY % {"lat": center[0], "lon": center[1], "radius": radius_km}
    url = f"{WIKIDATA_SPARQL}?{urllib.parse.urlencode({'query': q, 'format': 'json'})}"
    try:
        payload = _get_json(url)
    except Exception as exc:
        logger.warning(f"[Regional] Wikidata query failed: {exc}")
        return []

    out: List[Candidate] = []
    for b in payload.get("results", {}).get("bindings", []):
        name = clean_name(b.get("companyLabel", {}).get("value", ""))
        if not name:
            continue
        lat = lon = None
        if "coord" in b:                       # "Point(10.18 47.98)"
            try:
                lon_s, lat_s = b["coord"]["value"].strip("Point()").split()
                lat, lon = float(lat_s), float(lon_s)
            except Exception:
                pass
        emp = None
        if "employees" in b:
            try:
                emp = int(float(b["employees"]["value"]))
            except Exception:
                pass
        out.append(Candidate(
            name=name, source="wikidata",
            city=b.get("placeLabel", {}).get("value"),
            lat=lat, lon=lon, employees=emp,
            branche=b.get("industryLabel", {}).get("value"),
            website=b.get("website", {}).get("value"),
            source_url=b.get("company", {}).get("value"),
        ))
    logger.info(f"[Regional] Wikidata returned {len(out)} companies")
    return out


# ── Wikipedia categories ────────────────────────────────────────────────────

def from_wikipedia(categories: Optional[List[str]] = None) -> List[Candidate]:
    cats = categories or WIKIPEDIA_CATEGORIES
    seen, out = set(), []
    for cat in cats:
        params = urllib.parse.urlencode({
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Kategorie:{cat}", "cmlimit": "500",
            "cmtype": "page", "format": "json",
        })
        try:
            payload = _get_json(f"{WIKIPEDIA_API}?{params}", timeout=30)
        except Exception as exc:
            logger.warning(f"[Regional] Wikipedia category {cat!r} failed: {exc}")
            continue
        for m in payload.get("query", {}).get("categorymembers", []):
            title = m.get("title", "")
            name = clean_name(title)
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(Candidate(
                name=name, source="wikipedia",
                source_url=f"https://de.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                raw={"category": cat},
            ))
        time.sleep(0.3)     # courtesy; the API allows far more
    logger.info(f"[Regional] Wikipedia returned {len(out)} companies")
    return out


# ── OpenStreetMap / Overpass ────────────────────────────────────────────────

_OVERPASS_QUERY = """
[out:json][timeout:300];
(
  nwr["name"]["office"="company"](around:%(radius)d,%(lat)s,%(lon)s);
  nwr["name"]["man_made"="works"](around:%(radius)d,%(lat)s,%(lon)s);
  nwr["name"]["industrial"](around:%(radius)d,%(lat)s,%(lon)s);
);
out center tags;
"""


def from_osm(radius_km: int = 50, center=MEMMINGEN) -> List[Candidate]:
    q = _OVERPASS_QUERY % {"radius": radius_km * 1000,
                           "lat": center[0], "lon": center[1]}
    body = urllib.parse.urlencode({"data": q}).encode()

    payload = None
    for mirror in OVERPASS_MIRRORS:
        try:
            got = _get_json(mirror, data=body, timeout=300)
        except Exception as exc:
            logger.warning(f"[Regional] Overpass mirror {mirror} failed: {exc}")
            continue
        # An EMPTY result is treated as a failure, not as "there are no
        # factories near Memmingen". Observed 7 Aug: a mirror returned HTTP
        # 200 with zero elements while the others were timing out, which
        # silently produced "OSM returned 0 named sites" — indistinguishable
        # from a genuine answer, and it would have quietly cost the only
        # site-based source.
        if got.get("elements"):
            payload = got
            break
        logger.warning(f"[Regional] Overpass mirror {mirror} returned no elements "
                       f"— treating as a failure and trying the next mirror")
    if payload is None:
        logger.warning("[Regional] all Overpass mirrors failed or returned nothing "
                       "— skipping OSM. This loses the only SITE-based source "
                       "(the one that finds plants of foreign parents); re-run later.")
        return []

    out: List[Candidate] = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        name = clean_name(tags.get("name") or tags.get("operator") or "")
        if not name:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        out.append(Candidate(
            name=name, source="osm",
            city=tags.get("addr:city"),
            lat=lat, lon=lon,
            branche=tags.get("industrial") or tags.get("man_made") or tags.get("office"),
            website=tags.get("website") or tags.get("contact:website"),
            source_url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
            raw={"postcode": tags.get("addr:postcode")},
        ))
    logger.info(f"[Regional] OSM returned {len(out)} named sites")
    return out


# ── orchestration ───────────────────────────────────────────────────────────

SOURCES = {"wikidata": from_wikidata, "wikipedia": from_wikipedia, "osm": from_osm}


def discover(sources: Optional[List[str]] = None, radius_km: int = 50) -> List[Candidate]:
    """Run the requested sources and return raw (undeduped, unfiltered)
    candidates. Filtering and dedupe are separate steps so each can be
    measured on its own — see scripts/rc_discover.py."""
    chosen = sources or list(SOURCES)
    out: List[Candidate] = []
    for key in chosen:
        fn = SOURCES.get(key)
        if not fn:
            logger.warning(f"[Regional] unknown source {key!r} — skipping")
            continue
        try:
            out.extend(fn(radius_km=radius_km) if key != "wikipedia" else fn())
        except Exception as exc:
            logger.warning(f"[Regional] source {key!r} failed: {exc}")
    return out


def filter_candidates(cands: List[Candidate], *, radius_km: int = 50) -> dict:
    """
    Apply the junk gate and the geographic gate, reporting why things were
    dropped rather than silently shrinking the list.

    Candidates with no coordinates are KEPT as `needs_geocoding` rather than
    discarded — every Wikipedia candidate lands there, and throwing them away
    would discard the source entirely.
    """
    kept, junk, out_of_radius, needs_geo = [], [], [], []
    for c in cands:
        if not accept(c.name):
            junk.append(c)
            continue
        d = c.distance_km
        if d is None:
            needs_geo.append(c)
        elif d > radius_km:
            out_of_radius.append(c)
        else:
            kept.append(c)
    return {"kept": kept, "junk": junk,
            "out_of_radius": out_of_radius, "needs_geocoding": needs_geo}
