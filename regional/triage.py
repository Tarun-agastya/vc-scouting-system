"""
Triage tiers for the regional register (Phase RC-2, added 7 Aug).

Why this exists, measured rather than assumed: the first full discovery sweep
wrote 1,317 companies, of which **60** were in the 100-4000 employee band and
**1,221** had no headcount at all. Those 1,221 are not a uniform queue —
OpenStreetMap maps every named site, so the same "headcount unknown" bucket
contained a 900-employee Liebherr plant and `easy-page werbedesign`, a
one-person web-design shop. Enriching all of them would cost ~1,200 outbound
searches and hours of local GPU time, mostly to establish that a sole trader
is a sole trader.

The tier encodes a prior about size, using the one signal available before
enrichment: **which source found it**. Wikidata, Wikipedia and the team's own
sheet are all notability-filtered — something has to be somewhat significant
to be in them at all, which correlates with employing more than a handful of
people. OSM has no such filter, which is exactly why it has the best recall on
plants of foreign parents AND the worst precision on corner shops.

This is a prior, not a verdict: a tier-3 record is deprioritised, never
discarded. If a human filters to tier 3 and finds a real prospect, that costs
nothing but a click.
"""
from __future__ import annotations

from typing import Optional

from regional.filters import DEFAULT_MAX_EMPLOYEES, DEFAULT_MIN_EMPLOYEES

TIER_READY = 1        # headcount known, inside the band
TIER_ENRICH = 2       # notability-filtered source, headcount unknown
TIER_LOW_PRIOR = 3    # OSM-only, headcount unknown
TIER_OUT_OF_BAND = 9  # headcount known, outside the band

# Sources that carry an implicit notability filter, i.e. inclusion in them is
# itself weak evidence the company is not tiny.
# google_places is deliberately NOT here. Like OSM it has no notability
# filter — anyone can register a Business Profile — so an unsized Google hit
# is no more likely to be a 300-employee factory than a two-person workshop.
# Its value is recall, not precision, so it lands in the low-prior tier until
# enrichment establishes a headcount.
NOTABILITY_SOURCES = {"sheet", "wikidata", "wikipedia", "manual", "search"}

LABELS = {
    TIER_READY: "ready (headcount in band)",
    TIER_ENRICH: "enrich next (notable source, size unknown)",
    TIER_LOW_PRIOR: "low prior (OSM-only, size unknown)",
    TIER_OUT_OF_BAND: "out of band",
}


# A site this large is strong evidence of a substantial employer, and is
# treated as equivalent to coming from a notability-filtered source. Chosen
# from the measured distribution of 342 real buildings (7 Aug): the median is
# 3,021 m² and the 75th percentile 13,045 m², while everything above ~10,000
# in the sample was an unambiguous industrial site — Grob-Werke, Kögel,
# Peri, Daimler Buses, Liebherr, ZwickRoell, Käserei Champignon. Below it sit
# the one-person offices this tier exists to skip.
LARGE_FOOTPRINT_M2 = 10_000.0


def tier_for(*, employees: Optional[int], source: Optional[str],
             footprint_m2: Optional[float] = None,
             lo: int = DEFAULT_MIN_EMPLOYEES,
             hi: int = DEFAULT_MAX_EMPLOYEES) -> int:
    if employees is not None:
        return TIER_READY if lo <= employees <= hi else TIER_OUT_OF_BAND

    # A big mapped site earns the enrichment queue regardless of source. This
    # is what makes the OSM pile tractable: enriching all 978 would cost ~27h
    # of GPU, mostly to establish that sole traders are sole traders, whereas
    # the ~30% with a large footprint cost ~3h and are nearly all real
    # employers. An unknown footprint is NOT read as small.
    if footprint_m2 is not None and footprint_m2 >= LARGE_FOOTPRINT_M2:
        return TIER_ENRICH

    return TIER_ENRICH if (source or "") in NOTABILITY_SOURCES else TIER_LOW_PRIOR
