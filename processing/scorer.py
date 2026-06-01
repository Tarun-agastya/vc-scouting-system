"""
Deterministic startup enrichment scoring — Phase 3.

Computes three outputs for every Startup record:
  enrichment_score  (0–100) — how much verifiable data we have
  source_confidence (0–100) — how trustworthy that data is
  score_tier        (str)   — human-readable band label
  score_breakdown   (dict)  — full explainable JSON record

Design principles
-----------------
* Pure function of the record's current state — no side-effects, no DB writes.
  The caller (processing/storage.py) is responsible for persisting the result.
* Never raises — any unexpected exception returns a zero ScoringResult so the
  storage pipeline is never blocked by a scoring failure.
* Checks both the `founders` ORM relationship (future enrichment path) and
  `raw_data.founders` (current extraction path) so founder presence is
  counted correctly from day one.

Scoring formula (total 100 pts)
--------------------------------
  A  Source Quality & Diversity   max 35
  B  Profile Completeness         max 25
  C  Founder & Contact Presence   max 25
  D  Funding Evidence             max 15

Score tiers
-----------
  81–100  PRIORITY
  61–80   HIGH_QUALITY_LEAD
  41–60   INTERESTING
  21–40   EARLY_DISCOVERY
   0–20   WEAK_SIGNAL
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Startup

logger = logging.getLogger(__name__)

# ── Schema version — bump when formula changes to invalidate cached scores ───
SCHEMA_VERSION = 1

# ── Source-type weight tables ─────────────────────────────────────────────────

# Category A step 1: best source-type score
_SOURCE_TYPE_SCORE: Dict[str, int] = {
    "accelerator":            20,
    "incubator":              20,
    "university_hub":         20,
    "startup_network":        12,
    "intelligence_platform":  12,
    "newsletter":              8,
    "rss":                     4,
    "general":                 4,
}

# Source confidence base (how trustworthy is the primary source?)
_SOURCE_CONFIDENCE_BASE: Dict[str, int] = {
    "accelerator":            45,
    "incubator":              45,
    "university_hub":         45,
    "startup_network":        30,
    "intelligence_platform":  30,
    "newsletter":             20,
    "rss":                    10,
    "general":                10,
}

# Diversity bonus indexed by min(unique_sources, 4)
_DIVERSITY_BONUS:      List[int] = [0,  0,  6, 10, 15]
_CONF_DIVERSITY_BONUS: List[int] = [0,  0, 15, 25, 30]

# Score tier boundaries — checked in descending order
SCORE_TIERS = [
    (81, "PRIORITY"),
    (61, "HIGH_QUALITY_LEAD"),
    (41, "INTERESTING"),
    (21, "EARLY_DISCOVERY"),
    (0,  "WEAK_SIGNAL"),
]


# ── Return type ───────────────────────────────────────────────────────────────

@dataclass
class ScoringResult:
    enrichment_score:  int
    source_confidence: int
    score_tier:        str
    score_breakdown:   Dict[str, Any]


# ── Public API ────────────────────────────────────────────────────────────────

def compute_enrichment_score(record: "Startup") -> ScoringResult:
    """
    Compute enrichment_score, source_confidence, score_tier, and the full
    score_breakdown for *record*.

    Pure function of the record's current DB state.  Safe to call multiple
    times — repeated calls on unchanged data produce identical output.
    """
    try:
        return _compute(record)
    except Exception as exc:
        logger.error(
            f"[Scorer] Unexpected error for '{getattr(record, 'name', '?')}': {exc}"
        )
        return ScoringResult(
            enrichment_score=0,
            source_confidence=0,
            score_tier="WEAK_SIGNAL",
            score_breakdown={"version": SCHEMA_VERSION, "error": str(exc)},
        )


def tier_label(score: int) -> str:
    """Map a raw integer score to its tier label string."""
    for threshold, label in SCORE_TIERS:
        if score >= threshold:
            return label
    return "WEAK_SIGNAL"


def should_rescore(record: "Startup", new_source_url: str) -> bool:
    """
    Return True if *record* warrants re-scoring given *new_source_url*.

    Trigger conditions (any one is sufficient):
      1. Never been scored (last_enriched_at is None)
      2. new_source_url is not yet in source_history  → new evidence
      3. Score is stale (last_enriched_at > 7 days ago)

    Called by storage.upsert_startup() BEFORE mutating source_history so
    condition 2 correctly detects genuinely new source URLs.
    """
    if record.last_enriched_at is None:
        return True

    history = record.source_history or []
    known_urls = {entry.get("url") for entry in history}
    if new_source_url not in known_urls:
        return True

    age_days = (datetime.utcnow() - record.last_enriched_at).days
    if age_days > 7:
        return True

    return False


# ── Internal computation ──────────────────────────────────────────────────────

def _compute(record: "Startup") -> ScoringResult:
    history      = record.source_history or []
    source_types = [e.get("source", "general") for e in history] or ["general"]
    unique_urls  = {e.get("url", "") for e in history if e.get("url")}
    unique_sources = max(len(unique_urls), 1)  # floor at 1 (current source counts)

    # ── Category A: Source Quality & Diversity (max 35) ──────────────────────
    best_type_score = max(
        (_SOURCE_TYPE_SCORE.get(st, 4) for st in source_types), default=4
    )
    div_idx   = min(unique_sources, 4)
    div_bonus = _DIVERSITY_BONUS[div_idx]
    best_type = max(source_types, key=lambda st: _SOURCE_TYPE_SCORE.get(st, 4))
    cat_a_score = min(best_type_score + div_bonus, 35)

    cat_a: Dict[str, Any] = {
        "score":             cat_a_score,
        "max":               35,
        "best_source_type":  best_type,
        "best_source_score": best_type_score,
        "unique_sources":    unique_sources,
        "diversity_bonus":   div_bonus,
    }

    # ── Category B: Profile Completeness (max 25) ────────────────────────────
    has_website  = bool(record.website and "http" in record.website)
    desc_words   = len((record.description or "").split())
    has_desc     = desc_words >= 50
    has_industry = bool(record.industry)
    has_location = bool(record.city and record.country)
    founded_ok   = bool(
        record.founded_year and 1990 <= int(record.founded_year) <= 2030
    )
    tags_ok   = bool(record.tags and len(record.tags) >= 2)
    has_size  = bool(record.employee_count or record.business_model)

    cat_b_score = (
        (5 if has_website  else 0)
        + (5 if has_desc     else 0)
        + (4 if has_industry else 0)
        + (4 if has_location else 0)
        + (3 if founded_ok   else 0)
        + (2 if tags_ok      else 0)
        + (2 if has_size     else 0)
    )
    cat_b: Dict[str, Any] = {
        "score":             cat_b_score,
        "max":               25,
        "website":           has_website,
        "description_words": desc_words,
        "industry":          has_industry,
        "location":          has_location,
        "founded_year":      founded_ok,
        "tags_count":        len(record.tags or []),
        "size_signal":       has_size,
    }

    # ── Category C: Founder & Contact Presence (max 25) ──────────────────────
    has_linkedin = bool(record.linkedin and "linkedin.com" in record.linkedin)

    # Founder presence: check ORM relationship (future enrichment) first,
    # then fall back to raw_data.founders (current extraction path).
    orm_founders  = bool(record.founders)
    raw_founders: list = []
    if record.raw_data and isinstance(record.raw_data, dict):
        raw_founders = record.raw_data.get("founders") or []
    has_founders    = orm_founders or bool(raw_founders)
    founders_source = (
        "relationship" if orm_founders else ("raw_data" if raw_founders else "none")
    )

    has_email = bool(record.contact_info and "@" in record.contact_info)

    cat_c_score = (
        (10 if has_linkedin  else 0)
        + (8 if has_founders else 0)
        + (7 if has_email    else 0)
    )
    cat_c: Dict[str, Any] = {
        "score":           cat_c_score,
        "max":             25,
        "linkedin":        has_linkedin,
        "founders_present": has_founders,
        "founders_source": founders_source,
        "direct_contact":  has_email,
    }

    # ── Category D: Funding Evidence (max 15) ────────────────────────────────
    _stage_raw = (record.funding_stage or "").lower().strip()
    has_stage  = bool(_stage_raw and _stage_raw not in ("unknown", "null", "none", ""))
    has_amount = bool(record.total_funding_usd and record.total_funding_usd > 0)
    has_rounds = bool(record.funding_rounds)

    cat_d_score = (
        (5 if has_stage  else 0)
        + (6 if has_amount else 0)
        + (4 if has_rounds else 0)
    )
    cat_d: Dict[str, Any] = {
        "score":                cat_d_score,
        "max":                  15,
        "funding_stage":        has_stage,
        "funding_amount_usd":   has_amount,
        "funding_rounds_linked": has_rounds,
    }

    # ── Totals ────────────────────────────────────────────────────────────────
    enrichment_score = cat_a_score + cat_b_score + cat_c_score + cat_d_score

    # ── Source Confidence ─────────────────────────────────────────────────────
    best_conf_base = max(
        (_SOURCE_CONFIDENCE_BASE.get(st, 10) for st in source_types), default=10
    )
    conf_div_bonus = _CONF_DIVERSITY_BONUS[div_idx]

    non_null_count = sum([
        has_website, has_desc, has_industry, has_location,
        founded_ok, has_linkedin, has_founders, has_email, has_stage,
    ])
    conf_completeness_bonus = (
        10 if non_null_count >= 6 else (5 if non_null_count >= 3 else 0)
    )
    source_confidence = min(
        100, best_conf_base + conf_div_bonus + conf_completeness_bonus
    )

    tier = tier_label(enrichment_score)

    breakdown: Dict[str, Any] = {
        "version":          SCHEMA_VERSION,
        "computed_at":      datetime.utcnow().isoformat(),
        "enrichment_score": enrichment_score,
        "source_confidence": source_confidence,
        "score_tier":       tier,
        "categories": {
            "source_quality":       cat_a,
            "profile_completeness": cat_b,
            "founder_contact":      cat_c,
            "funding_evidence":     cat_d,
        },
        "source_confidence_detail": {
            "base":               best_conf_base,
            "diversity_bonus":    conf_div_bonus,
            "completeness_bonus": conf_completeness_bonus,
        },
    }

    return ScoringResult(
        enrichment_score=enrichment_score,
        source_confidence=source_confidence,
        score_tier=tier,
        score_breakdown=breakdown,
    )
