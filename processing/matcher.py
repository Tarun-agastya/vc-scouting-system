"""
Multi-signal entity matcher (Phase S-3).

Replaces the old name-only dedup (exact name+domain fingerprint, then a
name-only fuzzy fallback) with a layered match function that combines several
independent signals — fixing the two failure modes the old logic had:

  1. Same startup renamed, same website  -> old logic MISSED (name-change
     produces an unrelated fingerprint hash; the domain match was never
     checked on its own). Now caught by the Layer-1 exact-domain signal.
  2. Different startups sharing a name, no website -> old logic silently
     MIS-MERGED (name-only fuzzy match, no other check). Now the weighted
     score requires corroboration (location / founders / semantics), and
     ambiguous cases go to human review instead of auto-merging.

Layered design (cost-ordered; each layer only runs if the previous is
inconclusive):

  Layer 1  Deterministic, certain, cheap:
             - exact name+domain fingerprint     -> merge (confidence 1.0)
             - exact registrable-domain match     -> merge (confidence 0.97)
  Layer 2  Blocking (reuses Qdrant): embed the incoming record, retrieve the
           top-N most-similar existing startups as candidates. Avoids an
           all-pairs scan and sidesteps the "semantic neighbour = same entity"
           false-positive trap by only *shortlisting* here, not deciding.
  Layer 3  Weighted multi-signal score over the candidates:
             name + embedding + location + founded-year + founder-overlap.
             >= merge_threshold  -> merge
             >= review_threshold -> human review (insert new + flag the pair)
             else                -> new
  Layer 4  (optional, settings.dedup_llm_judge) local qwen adjudicates the
           review band before it reaches a human. Off by default.

All weights/thresholds live in config/__init__.py (Settings) so matching can
be calibrated against real data via .env with no code change.

Degrades safely: if embedding/Qdrant blocking fails, falls back to the old
name-only scan so ingestion never breaks because of a matcher error.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, List

from config import settings
from processing.deduplicator import (
    generate_fingerprint,
    extract_domain,
    normalize_company_name,
)

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    decision: str                       # "merge" | "new" | "review"
    matched_id: Optional[str] = None    # existing startup id for merge/review
    matched_name: Optional[str] = None
    confidence: float = 0.0
    signals: dict = field(default_factory=dict)
    reason: str = ""


# ── Per-signal scorers (each returns 0.0–1.0) ────────────────────────────────

def _founder_set(founders) -> set:
    """Normalize a founders value (list or str) into a lowercase name set."""
    if not founders:
        return set()
    if isinstance(founders, str):
        founders = [founders]
    out = set()
    for f in founders:
        if isinstance(f, str):
            n = f.strip().lower()
            if len(n) >= 4:  # skip initials/noise that cause false overlaps
                out.add(n)
    return out


def _founder_overlap(a: set, b: set) -> float:
    """Jaccard overlap of two founder-name sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _location_match(a: dict, b: dict) -> float:
    """1.0 if same city, 0.6 if only same country, 0.0 otherwise / unknown."""
    ac, bc = (a.get("city") or "").strip().lower(), (b.get("city") or "").strip().lower()
    if ac and bc and ac == bc:
        return 1.0
    aco = (a.get("country") or "").strip().lower()
    bco = (b.get("country") or "").strip().lower()
    if aco and bco and aco == bco:
        return 0.6
    return 0.0


def _founded_year_match(a, b) -> float:
    """1.0 if equal, 0.5 if within one year, 0.0 otherwise / unknown."""
    try:
        ay, by = int(a), int(b)
    except (TypeError, ValueError):
        return 0.0
    if ay == by:
        return 1.0
    if abs(ay - by) == 1:
        return 0.5
    return 0.0


def _name_similarity(a_name: str, b_name: str) -> float:
    """rapidfuzz token_sort_ratio on normalized names, scaled to 0.0–1.0."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return 0.0
    na, nb = normalize_company_name(a_name), normalize_company_name(b_name)
    if not na or not nb:
        return 0.0
    return fuzz.token_sort_ratio(na, nb) / 100.0


def _score_pair(incoming: dict, existing_row, embedding_sim: float) -> tuple:
    """
    Weighted multi-signal score for an (incoming, existing) pair.
    Returns (score, signals_dict). embedding_sim is the cosine similarity
    from the Qdrant blocking step (already 0.0–1.0).
    """
    existing = existing_row.raw_data or {}
    # Prefer structured columns on the row; fall back to raw_data.
    existing_view = {
        "city":         existing_row.city    or existing.get("city"),
        "country":      existing_row.country or existing.get("country"),
        "founded_year": existing_row.founded_year or existing.get("founded_year"),
        "founders":     existing.get("founders"),
    }

    name_sim    = _name_similarity(incoming.get("name", ""), existing_row.name or "")
    loc         = _location_match(incoming, existing_view)
    year        = _founded_year_match(incoming.get("founded_year"), existing_view["founded_year"])
    founders    = _founder_overlap(_founder_set(incoming.get("founders")),
                                   _founder_set(existing_view["founders"]))
    emb         = max(0.0, min(1.0, embedding_sim))

    score = (
        settings.dedup_weight_name         * name_sim +
        settings.dedup_weight_embedding    * emb +
        settings.dedup_weight_location     * loc +
        settings.dedup_weight_founded_year * year +
        settings.dedup_weight_founders     * founders
    )
    signals = {
        "name_similarity":  round(name_sim, 3),
        "embedding_sim":    round(emb, 3),
        "location_match":   round(loc, 3),
        "founded_year":     round(year, 3),
        "founder_overlap":  round(founders, 3),
    }
    return score, signals


# ── Deterministic Layer-1 helpers ────────────────────────────────────────────

def _exact_domain_match(website: str, db) -> Optional[object]:
    """
    Return an existing Startup whose website resolves to the same registrable
    domain, or None. A shared non-empty domain is near-proof of same company —
    this is the signal the old fingerprint bundled away and never checked alone,
    which is why renames were missed.
    """
    from database.models import Startup
    domain = extract_domain(website)
    if not domain:
        return None
    rows = db.query(Startup).filter(Startup.website.isnot(None)).all()
    for row in rows:
        if extract_domain(row.website) == domain:
            return row
    return None


# ── Public entry point ───────────────────────────────────────────────────────

def find_match(startup: dict, db, incoming_vector: Optional[List[float]] = None) -> MatchResult:
    """
    Decide whether `startup` matches an existing record.

    incoming_vector : the whole-record embedding of `startup` (reused from the
      caller so we don't embed twice). If None, Layer-2 blocking is skipped and
      we fall back to deterministic + a name-only scan.
    """
    name = (startup.get("name") or "").strip()
    website = startup.get("website") or ""

    # ── Layer 1a: exact name+domain fingerprint (trustworthy ONLY with a domain) ─
    # A fingerprint over a name with no domain is just a name hash — two
    # different companies sharing a name and having no website collide on it.
    # So we only auto-merge on a fingerprint hit when a real domain backs it;
    # otherwise the decision falls through to the multi-signal layers below.
    from database.models import Startup
    domain = extract_domain(website)
    if domain:
        fingerprint = generate_fingerprint(name, website)
        if fingerprint:
            row = db.query(Startup).filter(Startup.fingerprint == fingerprint).first()
            if row:
                return MatchResult("merge", str(row.id), row.name, 1.0,
                                   {"exact_fingerprint": 1.0}, "exact name+domain fingerprint")

    # ── Layer 1b: exact registrable-domain match (fixes the rename case) ─────
    if website:
        row = _exact_domain_match(website, db)
        if row:
            return MatchResult("merge", str(row.id), row.name, 0.97,
                               {"exact_domain": 1.0},
                               f"same domain '{extract_domain(website)}' as '{row.name}'")

    # ── Layer 2: blocking via Qdrant (shortlist candidates) ──────────────────
    candidates = _block_candidates(startup, incoming_vector, db)

    # ── Layer 3: weighted multi-signal score over candidates ─────────────────
    best_row, best_score, best_signals = None, 0.0, {}
    for row, emb_sim in candidates:
        score, signals = _score_pair(startup, row, emb_sim)
        if score > best_score:
            best_row, best_score, best_signals = row, score, signals

    if best_row is None:
        return MatchResult("new", None, None, 0.0, {}, "no candidates")

    # ── Layer 4 (optional): local-LLM judge for the review band ──────────────
    if (settings.dedup_review_threshold <= best_score < settings.dedup_merge_threshold
            and settings.dedup_llm_judge):
        verdict = _llm_judge(startup, best_row)
        if verdict is True:
            return MatchResult("merge", str(best_row.id), best_row.name, best_score,
                               best_signals, "llm judge: same company")
        if verdict is False:
            return MatchResult("new", None, None, best_score, best_signals,
                               "llm judge: different companies")
        # verdict None -> fall through to human review

    if best_score >= settings.dedup_merge_threshold:
        return MatchResult("merge", str(best_row.id), best_row.name, round(best_score, 3),
                           best_signals, "multi-signal score >= merge threshold")
    if best_score >= settings.dedup_review_threshold:
        return MatchResult("review", str(best_row.id), best_row.name, round(best_score, 3),
                           best_signals, "multi-signal score in review band")
    return MatchResult("new", None, None, round(best_score, 3), best_signals,
                       "below review threshold")


def _block_candidates(startup: dict, incoming_vector, db) -> list:
    """
    Return a shortlist of [(Startup row, embedding_similarity), ...] to score.

    Primary path: Qdrant nearest-neighbour search on the incoming vector.
    Fallback (embedding/Qdrant unavailable): name-only scan of all rows, with
    embedding_similarity = 0.0 — preserves the old behaviour so ingestion never
    breaks on a matcher error.
    """
    from database.models import Startup

    if incoming_vector is not None:
        try:
            from vector_db.qdrant_store import qdrant_store
            hits = qdrant_store.search_startups(
                query_vector=incoming_vector, limit=settings.dedup_block_top_n
            )
            out = []
            for h in hits:
                row = db.query(Startup).filter(Startup.id == str(h.id)).first()
                if row:
                    out.append((row, float(h.score)))
            if out:
                return out
        except Exception as exc:
            logger.warning(f"[Matcher] Qdrant blocking failed, falling back to name scan: {exc}")

    # Fallback: name-only candidate scan (no embedding signal available)
    name = (startup.get("name") or "").strip()
    if not name:
        return []
    rows = db.query(Startup).filter(
        Startup.normalized_name.isnot(None), Startup.normalized_name != ""
    ).all()
    scored = []
    for row in rows:
        sim = _name_similarity(name, row.name or "")
        if sim >= 0.5:  # only bother scoring plausible name matches
            scored.append((row, 0.0))
    return scored


def _llm_judge(startup: dict, existing_row) -> Optional[bool]:
    """
    Ask the local reasoning model whether two records are the same company.
    Returns True (same), False (different), or None (uncertain -> human review).
    Acquires no lock here — callers on the ingestion path already hold the GPU
    mutex via the controller. Never raises.
    """
    try:
        from reasoning.qwen_client import qwen_client
        a = f"{startup.get('name','')} — {startup.get('description','') or startup.get('one_liner','')} " \
            f"({startup.get('city','')}, {startup.get('country','')})"
        b = f"{existing_row.name} — {existing_row.description or existing_row.short_description or ''} " \
            f"({existing_row.city or ''}, {existing_row.country or ''})"
        prompt = (
            "Are these two records the SAME company? Answer with exactly one word: "
            "YES, NO, or UNSURE.\n\n"
            f"Record A: {a}\nRecord B: {b}"
        )
        resp = (qwen_client.generate(prompt) or "").strip().upper()
        if "YES" in resp:
            return True
        if "NO" in resp:
            return False
        return None
    except Exception as exc:
        logger.warning(f"[Matcher] LLM judge failed, deferring to human review: {exc}")
        return None
