"""
Candidate matching for the regional register (Phase RC-2).

Two jobs: collapse duplicates *between* sources (the same company arrives as
"Wanzl", "Wanzl GmbH" and "Wanzl (Unternehmen)"), and decide whether a
candidate is already in the register.

Reuses `processing.deduplicator.normalize_company_name` + rapidfuzz rather
than inventing a third matching rule — the project already dedupes startups
this way, and a register that disagreed with it about what counts as the same
company would be confusing to reason about.

The bar is deliberately a little looser than the startup matcher's: German
company names vary mostly by legal-form suffix ("Josef Hebel" vs "Josef Hebel
GmbH & Co. KG"), so `core_name` strips those before comparing. A false merge
here costs one missed prospect; a false split costs a duplicate row a human
has to notice. Neither is severe, and duplicates are visible in the dashboard.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

from processing.deduplicator import normalize_company_name
from regional.filters import core_name

logger = logging.getLogger(__name__)

# token_sort_ratio bar for "same company" on NAME ALONE, with no corroborating
# location. Deliberately high, because name-only evidence is weak in German:
# so many Mittelstand firms are built on a founder's surname that a moderate
# string similarity is genuinely ambiguous.
SIMILARITY_THRESHOLD = 88

# The fuzzy bar when a shared location corroborates the name, for pairs the
# token-subset rule does not already settle. Calibrated against real same-town
# pairs from the register rather than picked by feel:
#
#   should merge     Sensortechnik Wiedemann ~ Sensor-Technik Wiedemann  97.9
#                    Pfeiferseil u. Hebetechnik ~ Pfeifer Seil- u. Hebe. 98.2
#   should NOT       Andreas Ritzl Maschinenbau ~ Metzner Maschinenbau   65.2
#                    Weißenhorner Molkerei ~ AWB-MHKW Weißenhorn         65.0
#                    Hermann Rehklau Bauelemente ~ Betonsteinwerk Rehklau 61.2
#                    Merkle Schweißanlagen Technik ~ Bertele Metalltechnik 60.0
#
# 80 sits in the wide empty gap between those clusters. An earlier value of 60
# put it below the noise floor and merged all four false pairs.
SIMILARITY_THRESHOLD_SAME_CITY = 80

# Two sites this close are effectively the same location for matching purposes
# — a company's registered office and its plant are often a few streets apart,
# and geocoding a town name lands everyone on the town centre anyway.
_SAME_PLACE_KM = 12.0


def match_key(name: str) -> str:
    """Normalised comparison key: project normalization, then legal-form strip."""
    return core_name(normalize_company_name(name) or name or "")


def _norm_city(city) -> str:
    return " ".join(str(city or "").strip().lower().split())


def _same_place(a_city=None, b_city=None, a_coords=None, b_coords=None) -> Optional[bool]:
    """
    True / False / None (unknown). Location is only allowed to influence a
    decision when it is actually known for BOTH sides — an absent city must
    never be read as "different place", or every Wikipedia candidate (which
    carry no location at all) would be forced apart from everything.
    """
    ca, cb = _norm_city(a_city), _norm_city(b_city)
    if ca and cb:
        if ca == cb:
            return True
        # Fall through to coordinates rather than declaring different: the same
        # town is written "Kempten", "Kempten (Allgäu)" and "Kempten Allgaeu".
        if ca in cb or cb in ca:
            return True

    if a_coords and b_coords and all(v is not None for v in (*a_coords, *b_coords)):
        from regional.geocode import haversine_km
        return haversine_km(a_coords, b_coords) <= _SAME_PLACE_KM

    if ca and cb:
        return False
    return None


def is_same(a: str, b: str, *, a_city=None, b_city=None,
            a_coords=None, b_coords=None) -> bool:
    """
    Are these the same company?

    Name alone cannot decide this, and no threshold on name alone ever will —
    measured live 7 Aug, these two pairs are structurally identical as strings
    yet have opposite answers:

        "Urban GmbH"  vs "Urban Maschinenbau"          SAME (both Memmingen)
        "Schmid GmbH" vs "Hubert Schmid Bauunternehmen" DIFFERENT
                                        (Weiler-Simmerberg vs Marktoberdorf)

    A name-only matcher tuned to merge the first also merges the second (the
    original bug), and one tuned to split the second also splits the first
    (the bug that replaced it — it left 3M Ceramics / 3M Technical Ceramics,
    Autohaus Seitz / Seitz-Gruppe and Kolb Wellpappe / Hans Kolb Wellpappe as
    duplicate rows). Location is the signal that separates them, which is the
    same multi-signal reasoning processing/matcher.py applies to startups.

    Location is corroborating evidence only: it can lower the name bar when it
    agrees and raise it when it disagrees, but it is never consulted unless
    known for both sides, and a strong name match still wins on its own.
    """
    ka, kb = match_key(a), match_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True

    place = _same_place(a_city, b_city, a_coords, b_coords)
    shorter, longer = sorted((ka, kb), key=len)

    # Prefix containment: a brand shortening is essentially always a prefix
    # ("myonic" of "myonic gmbh", "Wanzl" of "Wanzl GmbH"), whereas a shared
    # surname sits in the middle.
    if len(shorter) >= 6 and (longer == shorter or longer.startswith(shorter + " ")):
        # ...but not across a known-different location: "Schmid" is a prefix of
        # "Schmid Recycling", and those can still be two different firms.
        return place is not False

    score = fuzz.token_sort_ratio(ka, kb)

    if place is True:
        # Same town lowers the bar, but ONLY for a containment-shaped pair:
        # every token of the shorter name must appear in the longer one.
        #
        # An earlier version accepted any shared word of 4+ characters, which
        # was catastrophically wrong — a dry run over the real register threw
        # up 220 "duplicates", nearly all false: "Allgäuer Zeitungsverlag" ~
        # "Allgäuer Überlandwerk", "Schreinerei Thomas Baumgartner" ~
        # "Schreinerei Hermann Rogg", "Amnesty International, Büro Ulm" ~
        # "Maschuthi, Büro für Gestaltung". German company names are built
        # from a small pool of generic words (schreinerei, büro, technik,
        # team, institut, molkerei, allgäuer), so a shared word carries almost
        # no identity information.
        #
        # Token-subset is the right shape: "urban" ⊆ "urban maschinenbau" and
        # "3m ceramics" ⊆ "3m technical ceramics" are one company written two
        # ways, whereas the false pairs above each share only a fraction of a
        # multi-token name.
        if _is_token_subset(ka, kb):
            return True
        # Otherwise the pair must share at least one whole DISTINCTIVE word,
        # and only then does the fuzzy score get a vote.
        #
        # A fuzzy score alone cannot work here, measured: the two clusters
        # overlap, so no threshold exists.
        #     should merge  Sensortechnik Wiedemann ~ Sensor-Technik W.  82.1
        #                   Franz Habisreutinger ~ Habisreutinger        82.4
        #     should NOT    FingerHaus ~ Fingerhut                       84.2
        #                   Halle 10 ~ Halle 11                          87.5
        # What separates them is not how similar the characters are but
        # whether an actual word is shared: "wiedemann" and "habisreutinger"
        # are common to their pairs, whereas FingerHaus/Fingerhut and
        # Halle 10/Halle 11 merely look alike letter by letter.
        sa, sb = _strip_generic(ka), _strip_generic(kb)
        if not sa or not sb:
            return False
        if not (set(sa.split()) & set(sb.split())):
            return False
        return fuzz.token_sort_ratio(sa, sb) >= SIMILARITY_THRESHOLD_SAME_CITY

    if place is False:
        # Known-different towns: demand a near-exact name before merging.
        return score >= 95

    return score >= SIMILARITY_THRESHOLD


# Words that appear in a great many German company names and therefore carry
# essentially no identity information. A pair whose ONLY overlap is these is
# not evidence of anything — see the false pairs listed in is_same().
_GENERIC_TOKENS = {
    "schreinerei", "buro", "büro", "technik", "team", "institut", "molkerei",
    "allgauer", "allgäuer", "allgau", "allgäu", "schwaben", "bayern",
    "bau", "haus", "werk", "werke", "service",
    "system", "systeme", "maschinenbau", "verwaltung", "zentrale", "nord",
    "sud", "süd", "west", "ost", "neu", "alt", "gebaude", "gebäude",
    "gesellschaft", "betrieb", "betriebe", "handel", "produktion", "und",
    "halle", "tor", "pforte", "lager", "eingang", "gruppe", "group",
    # Business descriptors, German and English. "Wenger Engineering" and
    # "euro engineering" are unrelated Ulm firms that merged on the shared
    # word "engineering" alone (7 Aug) — the same failure mode as the
    # "Schreinerei"/"Büro" pairs, just in a different vocabulary.
    "engineering", "elektronik", "electronics", "logistik", "logistics",
    "consulting", "solutions", "systems", "industrie", "industrial",
    "software", "digital", "energie", "energy", "medizin", "medical",
    "international", "zentrum", "center", "centrum", "partner", "partners",
    "fur", "für", "der", "die", "das", "am", "im", "gmbh", "kg", "ag",
}


def _is_token_subset(ka: str, kb: str) -> bool:
    """
    True when every token of the shorter name also appears in the longer one,
    and the overlap is not purely generic.

    "urban"        ⊆ "urban maschinenbau"       -> True
    "3m ceramics"  ⊆ "3m technical ceramics"    -> True
    "seitz"        ⊆ "autohaus seitz"           -> True
    "allgauer zeitungsverlag" vs "allgauer uberlandwerk" -> False (not a
                                                            subset)
    "schreinerei x" vs "schreinerei y"          -> False (not a subset)
    """
    ta, tb = ka.split(), kb.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short:
        return False
    long_set = set(long_)
    if not all(t in long_set for t in short):
        return False
    # Require at least one distinctive token in the shared part — otherwise
    # "Technik" ⊆ "Greif Technik Schmid" would merge on a descriptor alone.
    return any(t not in _GENERIC_TOKENS for t in short)


def _strip_generic(key: str) -> str:
    """Drop generic/region words, leaving only the distinctive part of a name.
    'allgäu mail' -> 'mail'; 'andreas ritzl maschinenbau' -> 'andreas ritzl'."""
    return " ".join(t for t in key.split() if t not in _GENERIC_TOKENS)


def find_match(name: str, pool: Iterable, *, city=None, coords=None) -> Optional[str]:
    """
    `pool` may be plain names, or (name, city, coords) tuples when location is
    available — callers that have it should pass it, since it is what makes
    the decision reliable.
    """
    for other in pool:
        if isinstance(other, (tuple, list)):
            o_name = other[0]
            o_city = other[1] if len(other) > 1 else None
            o_coords = other[2] if len(other) > 2 else None
        else:
            o_name, o_city, o_coords = other, None, None
        if is_same(name, o_name, a_city=city, b_city=o_city,
                   a_coords=coords, b_coords=o_coords):
            return o_name
    return None


def dedupe(candidates: List) -> Tuple[List, int]:
    """
    Collapse candidates that refer to the same company, preferring the record
    that carries the most information (coordinates and an employee count beat
    a bare name), and merging in fields the winner lacks.

    Returns (deduped, collapsed_count).
    """
    def richness(c) -> tuple:
        return (c.employees is not None, c.lat is not None,
                bool(c.website), bool(c.branche), len(c.name))

    ordered = sorted(candidates, key=richness, reverse=True)
    kept: List = []
    collapsed = 0

    for cand in ordered:
        hit = None
        for k in kept:
            if is_same(cand.name, k.name,
                       a_city=cand.city, b_city=k.city,
                       a_coords=(cand.lat, cand.lon) if cand.lat is not None else None,
                       b_coords=(k.lat, k.lon) if k.lat is not None else None):
                hit = k
                break
        if hit is None:
            kept.append(cand)
            continue

        collapsed += 1
        # Fill gaps in the richer record from the poorer one; never overwrite.
        for attr in ("city", "lat", "lon", "employees", "branche", "website"):
            if getattr(hit, attr, None) in (None, "") and getattr(cand, attr, None):
                setattr(hit, attr, getattr(cand, attr))
        srcs = set((hit.raw or {}).get("also_seen_in", [])) | {cand.source}
        hit.raw = {**(hit.raw or {}), "also_seen_in": sorted(srcs)}

    return kept, collapsed


def split_new_vs_known(candidates: List, existing: Iterable) -> Tuple[List, List]:
    """
    Partition candidates into (new, already_in_register).

    `existing` should be (name, city, coords) tuples so location can
    corroborate — passing bare names still works but reverts to the weaker
    name-only bar, which is what left 3M Ceramics / 3M Technical Ceramics as
    two rows.
    """
    pool = list(existing)
    new, known = [], []
    for c in candidates:
        coords = (c.lat, c.lon) if getattr(c, "lat", None) is not None else None
        if find_match(c.name, pool, city=getattr(c, "city", None), coords=coords):
            known.append(c)
        else:
            new.append(c)
    return new, known
