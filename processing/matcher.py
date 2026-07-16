"""
Data-stewardship entity matcher (Phase S-3b).

The matcher is an EVIDENCE-GATHERING machine, not a guessing machine. It never
merges or overwrites — it classifies the identity relationship between an
incoming startup and the existing masters, and returns a full per-signal
evidence scorecard. `processing/storage.py` turns that report into a staged
review for a human; nothing is auto-applied except recognizing an exact
same-record fingerprint and no-op'ing when nothing changed.

Key principles (see DATA_INTEGRITY_PLAN.md addendum §Phase S-3b):
  - Run ALL layers regardless of early matches; aggregate into `evidence`.
  - Pattern-based decision, not a single linear threshold — every signal is
    stored separately and the outcome keys off signal *patterns*.
  - Shared-domain trap fix: a multi-tenant domain (linkedin.com, medium.com,
    github.io, …) is never treated as an identity signal; and a domain match
    contradicted by all other signals is an `anomaly`, not a match.
  - Hybrid blocking: Qdrant embedding shortlist, widened, with structured
    signals (location, founded-year) demoting same-description competitors in
    the score (soft — never a hard filter that could drop a sparse true dupe).

Outcomes (identity classification only — storage decides no_op vs staged_update):
  exact_same_record | possible_duplicate | anomaly | no_match

Degrades safely: if embedding/Qdrant blocking fails, falls back to a name-only
scan so ingestion never breaks because of a matcher error.
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
class MatchReport:
    outcome: str                        # exact_same_record | possible_duplicate | anomaly | no_match
    master_id: Optional[str] = None     # the existing master this concerns (None for no_match)
    master_name: Optional[str] = None
    confidence: float = 0.0             # aggregate score — ordering only, NOT a gate
    risk_level: str = "none"            # low | high | anomaly | none
    evidence: dict = field(default_factory=dict)   # full per-signal scorecard
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


def _score_pair(incoming: dict, existing_row, embedding_sim: float, domain_match: bool) -> tuple:
    """
    Full evidence for an (incoming, existing) pair: every signal separately +
    a weighted aggregate (used only for ordering/confidence, never as the sole
    decision gate). embedding_sim is the Qdrant cosine similarity (0.0–1.0).
    """
    existing = existing_row.raw_data or {}
    existing_view = {
        "city":         existing_row.city    or existing.get("city"),
        "country":      existing_row.country or existing.get("country"),
        "founded_year": existing_row.founded_year or existing.get("founded_year"),
        "founders":     existing.get("founders"),
    }

    name_sim = _name_similarity(incoming.get("name", ""), existing_row.name or "")
    loc      = _location_match(incoming, existing_view)
    year     = _founded_year_match(incoming.get("founded_year"), existing_view["founded_year"])
    founders = _founder_overlap(_founder_set(incoming.get("founders")),
                                _founder_set(existing_view["founders"]))
    emb      = max(0.0, min(1.0, embedding_sim))

    score = (
        settings.dedup_weight_name         * name_sim +
        settings.dedup_weight_embedding    * emb +
        settings.dedup_weight_location     * loc +
        settings.dedup_weight_founded_year * year +
        settings.dedup_weight_founders     * founders
    )
    evidence = {
        "name_similarity": round(name_sim, 3),
        "embedding_sim":   round(emb, 3),
        "location_match":  round(loc, 3),
        "founded_year":    round(year, 3),
        "founder_overlap": round(founders, 3),
        "domain_match":    1.0 if domain_match else 0.0,
        "aggregate_score": round(score, 3),
    }
    return score, evidence


# ── Domain helpers (blocklist-aware) ─────────────────────────────────────────

def _multitenant_domains() -> set:
    return {d.strip().lower() for d in settings.dedup_multitenant_domains.split(",") if d.strip()}


def _identity_domain(website: str) -> Optional[str]:
    """
    Registrable domain usable as an IDENTITY signal, or None. Returns None for
    empty websites and for multi-tenant/shared domains (linkedin.com, etc.) —
    those are shared by many unrelated startups and must not imply same-company.
    """
    domain = extract_domain(website)
    if not domain:
        return None
    if domain in _multitenant_domains():
        return None
    return domain


# ── Candidate blocking (hybrid: embedding shortlist ∪ identity-domain rows) ────

def _block_candidates(startup: dict, incoming_vector, db) -> list:
    """
    Shortlist of [(Startup row, embedding_similarity), ...] to score.

    Primary: Qdrant nearest-neighbour on the incoming vector (widened top-N).
    Union'd with: any existing row sharing the incoming identity-domain (so a
    rename with the same website is always a candidate even if its description
    drifted out of the embedding neighbourhood).
    Fallback (embedding/Qdrant unavailable): name-only scan.
    """
    from database.models import Startup

    candidates = {}  # id -> (row, emb_sim)

    if incoming_vector is not None:
        try:
            from vector_db.qdrant_store import qdrant_store
            hits = qdrant_store.search_startups(
                query_vector=incoming_vector, limit=settings.dedup_block_top_n
            )
            for h in hits:
                row = db.query(Startup).filter(Startup.id == str(h.id)).first()
                if row:
                    candidates[str(row.id)] = (row, float(h.score))
        except Exception as exc:
            logger.warning(f"[Matcher] Qdrant blocking failed, falling back to name scan: {exc}")

    # Union in identity-domain matches (rename safety)
    idomain = _identity_domain(startup.get("website") or "")
    if idomain:
        for row in db.query(Startup).filter(Startup.website.isnot(None)).all():
            if extract_domain(row.website) == idomain and str(row.id) not in candidates:
                candidates[str(row.id)] = (row, 0.0)

    if candidates:
        return list(candidates.values())

    # Fallback: name-only candidate scan
    name = (startup.get("name") or "").strip()
    if not name:
        return []
    scored = []
    for row in db.query(Startup).filter(
        Startup.normalized_name.isnot(None), Startup.normalized_name != ""
    ).all():
        if _name_similarity(name, row.name or "") >= 0.5:
            scored.append((row, 0.0))
    return scored


# ── Pattern-based classification (not a single linear threshold) ─────────────

def _classify(evidence: dict, domain_match: bool) -> tuple:
    """
    Decide (outcome, risk_level, reason) from signal *patterns*. Returns one of
    possible_duplicate / anomaly / no_match (exact_same_record is handled
    earlier by the fingerprint check).
    """
    STRONG = settings.dedup_strong_signal
    GAP    = settings.dedup_anomaly_gap

    name = evidence["name_similarity"]
    emb  = evidence["embedding_sim"]
    fnd  = evidence["founder_overlap"]
    loc  = evidence["location_match"]
    score = evidence["aggregate_score"]

    corroborators = [name, emb, fnd, loc]
    max_corrob = max(corroborators)
    num_strong = sum(1 for c in corroborators if c >= STRONG)

    if domain_match:
        # Same real, non-shared domain but NOT an exact name+domain fingerprint.
        # If nothing else agrees, it's the shared-domain trap → anomaly.
        if max_corrob < GAP:
            return "anomaly", "anomaly", "domain matches but no other signal corroborates (possible shared domain)"
        return "possible_duplicate", "high", "same domain plus corroborating signals (likely rename)"

    # No identity-domain signal — rely on the other evidence.
    if fnd >= STRONG and (name >= STRONG or emb >= STRONG):
        return "possible_duplicate", "high", "shared founder + strong name/description match"
    if name >= STRONG and emb >= STRONG and loc >= 0.6:
        return "possible_duplicate", "high", "strong name + description + location agreement"
    if num_strong >= 2:
        return "possible_duplicate", "low", "two or more moderately strong signals"
    if score >= settings.dedup_review_threshold:
        return "possible_duplicate", "low", "aggregate score in review band"
    return "no_match", "none", "insufficient evidence"


# ── Public entry point ───────────────────────────────────────────────────────

def build_match_report(startup: dict, db, incoming_vector: Optional[List[float]] = None) -> MatchReport:
    """
    Gather evidence and classify the identity relationship of `startup` to the
    existing masters. Never merges. See module docstring for outcomes.
    """
    name = (startup.get("name") or "").strip()
    website = startup.get("website") or ""

    from database.models import Startup

    # ── Exact same-record: name + real, non-shared domain fingerprint ────────
    idomain = _identity_domain(website)
    if idomain:
        fingerprint = generate_fingerprint(name, website)
        if fingerprint:
            row = db.query(Startup).filter(Startup.fingerprint == fingerprint).first()
            if row:
                _, ev = _score_pair(startup, row, 1.0, domain_match=True)
                return MatchReport("exact_same_record", str(row.id), row.name, 1.0,
                                   "low", ev, "exact name+domain fingerprint")

    # ── Run all layers: block → score every candidate → best ─────────────────
    candidates = _block_candidates(startup, incoming_vector, db)
    best_row, best_score, best_ev, best_domain = None, -1.0, {}, False
    for row, emb_sim in candidates:
        dmatch = bool(idomain) and extract_domain(row.website or "") == idomain
        score, ev = _score_pair(startup, row, emb_sim, domain_match=dmatch)
        # Prefer the highest aggregate; a domain match is a strong tie-break.
        rank = score + (0.5 if dmatch else 0.0)
        if rank > best_score:
            best_row, best_score, best_ev, best_domain = row, rank, ev, dmatch

    if best_row is None:
        return MatchReport("no_match", None, None, 0.0, "none", {}, "no candidates")

    outcome, risk, reason = _classify(best_ev, best_domain)
    if outcome == "no_match":
        return MatchReport("no_match", None, None, best_ev.get("aggregate_score", 0.0),
                           "none", best_ev, reason)
    return MatchReport(outcome, str(best_row.id), best_row.name,
                       best_ev.get("aggregate_score", 0.0), risk, best_ev, reason)
