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

# token_sort_ratio bar for "same company". 88 chosen against the real data:
# it merges "Sensortechnik Wiedemann" / "Sensor-Technik Wiedemann GmbH (STW)"
# while keeping "Schmid GmbH" and "Hubert Schmid Recycling" apart.
SIMILARITY_THRESHOLD = 88


def match_key(name: str) -> str:
    """Normalised comparison key: project normalization, then legal-form strip."""
    return core_name(normalize_company_name(name) or name or "")


def is_same(a: str, b: str) -> bool:
    ka, kb = match_key(a), match_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True

    # Containment catches brand-vs-legal-name, but ONLY as a prefix at a word
    # boundary. Plain "is the shorter name inside the longer one" is far too
    # loose for German company names, which are commonly built on a surname:
    # it merged "Schmid GmbH" into "Hubert Schmid Bauunternehmen" (two
    # genuinely different companies, both real rows on the membership sheet).
    # A brand shortening is essentially always a prefix — "myonic" of "myonic
    # gmbh", "Wanzl" of "Wanzl GmbH" — whereas a shared surname sits in the
    # middle. Requiring prefix position keeps the former and rejects the latter.
    shorter, longer = sorted((ka, kb), key=len)
    if len(shorter) >= 6 and (longer == shorter or longer.startswith(shorter + " ")):
        return True

    return fuzz.token_sort_ratio(ka, kb) >= SIMILARITY_THRESHOLD


def find_match(name: str, pool: Iterable[str]) -> Optional[str]:
    for other in pool:
        if is_same(name, other):
            return other
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
            if is_same(cand.name, k.name):
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


def split_new_vs_known(candidates: List, existing_names: Iterable[str]) -> Tuple[List, List]:
    """Partition candidates into (new, already_in_register)."""
    existing = list(existing_names)
    new, known = [], []
    for c in candidates:
        if find_match(c.name, existing):
            known.append(c)
        else:
            new.append(c)
    return new, known
