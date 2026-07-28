"""
Thesis relevance ranking (Phase V-3, 24 Jul).

A "thesis" (stakeholder or ad-hoc theme, config/theses.yaml) is ranked
against startups via a semantic + tag hybrid: embed the thesis summary once,
compare it to each candidate startup's already-stored Qdrant vector via
cosine similarity, then boost the score when the startup's controlled
industry/tech_cluster or literal keywords match the thesis. Reuses the
vector already computed at ingest time — no re-embedding of startups.

Deliberately a hybrid, not pure tag-match: the owner's own complaint was
that industry/tech_cluster tags are noisy, so an imperfectly-classified
startup must still be able to rank on semantic closeness alone. Boost
weights are modest relative to the semantic base (cosine similarity, which
for related content typically falls in a ~0.3-0.8 band) so tags lift a
result without letting a single tag hit dominate a poor semantic match.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List

from config.thesis_loader import get_thesis

logger = logging.getLogger(__name__)

PRIMARY_TAG_BOOST = 0.18
PRIMARY_KEYWORD_BOOST = 0.04
PRIMARY_KEYWORD_BOOST_CAP = 0.20
SECONDARY_TAG_BOOST = 0.06
SECONDARY_KEYWORD_BOOST = 0.015
SECONDARY_KEYWORD_BOOST_CAP = 0.08
EXCLUDE_PENALTY = 0.25
EXCLUDE_PENALTY_CAP = 0.5

# thesis_id -> (summary_text, vector) — invalidated when the summary text
# itself changes (hot-reload friendly, since theses.yaml is mtime-cached
# upstream and a config edit changes the summary string we key on here).
_vector_cache: Dict[str, tuple] = {}


def _thesis_vector(thesis: dict) -> List[float]:
    from embeddings.embedder import embedder

    cached = _vector_cache.get(thesis["id"])
    if cached and cached[0] == thesis["summary"]:
        return cached[1]

    vector = embedder.embed(thesis["summary"])
    _vector_cache[thesis["id"]] = (thesis["summary"], vector)
    return vector


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _text_hits(haystack: str, needles: List[str]) -> List[str]:
    haystack_lower = haystack.lower()
    return [kw for kw in needles if kw and kw.lower() in haystack_lower]


def rank(thesis_id: str, startups: List[dict]) -> List[dict]:
    """
    startups: list of dicts, each with at least id/industry/tech_cluster/
    name/short_description/description, plus "vector" (the startup's
    stored Qdrant embedding, fetched by the caller via
    qdrant_store.get_vectors — reused, never recomputed here).

    Returns the same dicts with "relevance_score" and "matched_signals"
    added, sorted descending. A startup with no vector (never indexed)
    scores 0 on the semantic half but can still surface via tag/keyword
    boosts alone — never silently dropped.

    Raises ValueError if thesis_id is unknown/misspelled.
    """
    thesis = get_thesis(thesis_id)
    if thesis is None:
        raise ValueError(f"Unknown thesis id: {thesis_id!r}")

    thesis_vec = _thesis_vector(thesis)

    industries = set(thesis["industries"])
    clusters = set(thesis["tech_clusters"])
    sec_industries = set(thesis["secondary_industries"])
    sec_clusters = set(thesis["secondary_tech_clusters"])

    ranked = []
    for s in startups:
        signals: List[str] = []
        vector = s.get("vector")
        score = _cosine(thesis_vec, vector) if vector else 0.0

        industry = s.get("industry")
        cluster = s.get("tech_cluster")
        text = " ".join(filter(None, [
            s.get("name"), s.get("short_description"), s.get("description"),
        ]))

        if industry and industry in industries:
            score += PRIMARY_TAG_BOOST
            signals.append(f"industry: {industry}")
        if cluster and cluster in clusters:
            score += PRIMARY_TAG_BOOST
            signals.append(f"tech cluster: {cluster}")

        kw_hits = _text_hits(text, thesis["keywords"])
        if kw_hits:
            score += min(PRIMARY_KEYWORD_BOOST * len(kw_hits), PRIMARY_KEYWORD_BOOST_CAP)
            shown = ", ".join(kw_hits[:3])
            more = f" +{len(kw_hits) - 3} more" if len(kw_hits) > 3 else ""
            signals.append(f"keywords: {shown}{more}")

        if industry and industry in sec_industries:
            score += SECONDARY_TAG_BOOST
            signals.append(f"secondary industry: {industry}")
        if cluster and cluster in sec_clusters:
            score += SECONDARY_TAG_BOOST
            signals.append(f"secondary tech cluster: {cluster}")

        sec_kw_hits = _text_hits(text, thesis["secondary_keywords"])
        if sec_kw_hits:
            score += min(SECONDARY_KEYWORD_BOOST * len(sec_kw_hits), SECONDARY_KEYWORD_BOOST_CAP)
            signals.append(f"secondary keywords: {', '.join(sec_kw_hits[:3])}")

        exc_hits = _text_hits(text, thesis["exclude_keywords"])
        if exc_hits:
            score -= min(EXCLUDE_PENALTY * len(exc_hits), EXCLUDE_PENALTY_CAP)
            signals.append(f"excluded terms: {', '.join(exc_hits[:3])}")

        ranked.append({**s, "relevance_score": round(score, 4), "matched_signals": signals})

    ranked.sort(key=lambda r: r["relevance_score"], reverse=True)
    return ranked
