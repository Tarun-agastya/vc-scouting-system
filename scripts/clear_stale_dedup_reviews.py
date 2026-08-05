"""
Re-verify the pending possible_duplicate/anomaly backlog against the CURRENT
matcher, and reject only the reviews that no longer qualify.

Why this exists (4 Aug): two dedup-scoring changes shipped the same day —
location dropped to zero weight, and a new literal-text veto added (see
processing/matcher.py::_classify). Both govern *new* incoming records only;
reviews already staged under the old scoring keep sitting in the queue.
Measured on the live backlog, ~77% of comparable pending duplicate reviews
were staged by a mechanism that no longer exists.

Verification, not a blanket clear
---------------------------------
Every review is individually re-classified through the real, shipped
_classify() before anything happens to it:

  - Evidence is RECOMPUTED fresh via _score_pair() against the live records,
    not read from the stored evidence blob. This matters: the stored
    aggregate_score was computed with the old location weight (0.15) baked
    in, so trusting it would re-apply the very bias this cleanup exists to
    undo. embedding_sim and domain_match are carried over from the stored
    evidence — they are raw signals, unaffected by any weight change, and
    embedding_sim would otherwise need a Qdrant round-trip per record.
  - A review is only rejected when the current matcher returns "no_match"
    AND its name similarity is below NAME_FLOOR. The extra floor is a
    deliberate safety margin for this retroactive pass: _name_similarity()
    uses only rapidfuzz token_sort_ratio, which is blind to spacing and
    containment, so real duplicates can score deceptively low — measured
    live, "Quantum Diamonds ~ QuantumDiamonds" scores 0.516 (1.000 with
    whitespace removed) and "Bugsense Diagnostics ~ Bugsense" scores 0.571
    (1.000 by token_set_ratio). That is a pre-existing matcher weakness,
    previously masked by location's weight. Rather than let a bulk cleanup
    silently discard those, anything with meaningful name overlap is kept
    for a human even when the matcher would now pass on it. Clear false
    positives sit far below the floor (e.g. "Vaeridion ~ Bai Soft" 0.353,
    "AI Nation ~ Trade Republic" 0.261) and are unaffected.

Rejection goes through the same suppression-recording logic as the app's own
POST /reviews/{id}/reject (api/routes/reviews.py::_do_reject): status becomes
"rejected" with a resolved_at timestamp, and a SuppressedMatch
(kind="known_different") is recorded so the pair is never re-flagged. Nothing
is hard-deleted and no startup record is touched — the audit trail is intact
and every decision stays reversible.

Usage
-----
    python scripts/clear_stale_dedup_reviews.py            # dry run (default)
    python scripts/clear_stale_dedup_reviews.py --apply    # execute
    python scripts/clear_stale_dedup_reviews.py --verbose  # list every decision
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from processing.matcher import _score_pair, _classify

_PAIR_TYPES = ("possible_duplicate", "anomaly")

# Keep-for-human floor — see the module docstring. Set above the highest
# observed clear false positive (~0.44) and below the lowest observed genuine
# duplicate (0.516), so it separates the two clusters actually present in the
# live backlog rather than being an arbitrary round number.
NAME_FLOOR = 0.45

# Second keep-guard, targeting token_sort_ratio's two specific blind spots
# directly rather than by lowering NAME_FLOOR (which would sweep in real
# false positives sitting at 0.42-0.44):
#   token_set_ratio  -> containment ("goNEON" inside "goNEON Agentic Systems",
#                       "MAUD" inside "MyAutoData (MAUD)") = 1.000
#   whitespace-free  -> spacing ("Quantum Diamonds" vs "QuantumDiamonds") = 1.000
# Measured on the live backlog this separates the clusters perfectly: every
# genuine duplicate scores 1.000 here, every clear false positive <= 0.444.
NAME_ALT_FLOOR = 0.9


def _alt_name_similarity(a: str, b: str) -> float:
    """max(token_set_ratio, whitespace-stripped ratio), 0.0-1.0."""
    try:
        from rapidfuzz import fuzz
        from processing.deduplicator import normalize_company_name
    except ImportError:
        return 0.0
    na, nb = normalize_company_name(a or ""), normalize_company_name(b or "")
    if not na or not nb:
        return 0.0
    return max(
        fuzz.token_set_ratio(na, nb),
        fuzz.ratio(na.replace(" ", ""), nb.replace(" ", "")),
    ) / 100.0


def _incoming_dict(row: Startup) -> dict:
    """Rebuild the extraction-shaped dict _score_pair expects from a live row."""
    raw = row.raw_data or {}
    return {
        "name": row.name,
        "description": row.description or raw.get("description"),
        "city": row.city or raw.get("city"),
        "country": row.country or raw.get("country"),
        "founded_year": row.founded_year or raw.get("founded_year"),
        "founders": raw.get("founders"),
    }


def run(apply: bool, verbose: bool) -> None:
    db = SessionLocal()
    try:
        reviews = (
            db.query(DuplicateReview)
            .filter(DuplicateReview.status == "pending")
            .filter(DuplicateReview.review_type.in_(_PAIR_TYPES))
            .all()
        )

        stale, kept, orphaned, unscoreable, name_floor_kept = [], [], [], [], []

        for r in reviews:
            master = db.query(Startup).filter(Startup.id == r.master_id).first() if r.master_id else None
            incoming = db.query(Startup).filter(Startup.id == r.incoming_id).first() if r.incoming_id else None

            if master is None or incoming is None:
                # One side was deleted (e.g. by a junk cleanup) — the review
                # can no longer be judged. Reported, never auto-resolved.
                orphaned.append(r)
                continue

            stored = r.evidence or {}
            if "embedding_sim" not in stored:
                # Pre-dates the evidence scorecard, or was written by a path
                # that didn't store it — re-scoring would need a Qdrant
                # round-trip. Left alone rather than guessed at.
                unscoreable.append(r)
                continue

            domain_match = bool(stored.get("domain_match"))
            _, evidence = _score_pair(
                _incoming_dict(incoming), master,
                embedding_sim=float(stored.get("embedding_sim") or 0.0),
                domain_match=domain_match,
            )
            outcome, _, reason = _classify(evidence, domain_match=domain_match)

            alt_name = _alt_name_similarity(master.name, incoming.name)
            if outcome != "no_match":
                kept.append((r, master.name, incoming.name, outcome))
            elif (evidence.get("name_similarity", 0.0) >= NAME_FLOOR
                  or alt_name >= NAME_ALT_FLOOR):
                name_floor_kept.append((r, master.name, incoming.name, evidence, alt_name))
            else:
                stale.append((r, master.name, incoming.name, evidence, reason))

        print(f"Pending {'/'.join(_PAIR_TYPES)} reviews: {len(reviews)}")
        print(f"  REJECT — no_match and name_similarity < {NAME_FLOOR}   : {len(stale)}")
        print(f"  Keep — still qualifies as a duplicate/anomaly     : {len(kept)}")
        print(f"  Keep — no_match but names still look related      : {len(name_floor_kept)}")
        print(f"  Keep — orphaned, one side deleted (review by hand): {len(orphaned)}")
        print(f"  Keep — no stored embedding_sim to re-score        : {len(unscoreable)}")

        if verbose and stale:
            print("\nWould reject:")
            for _, mn, inn, ev, reason in stale:
                print(f"  overlap={ev.get('text_overlap')} name={ev.get('name_similarity')} "
                      f"| {mn} ~ {inn} | {reason}")
        if verbose and kept:
            print("\nWould KEEP (still classifies as duplicate/anomaly):")
            for _, mn, inn, outcome in kept:
                print(f"  [{outcome}] {mn} ~ {inn}")
        if verbose and name_floor_kept:
            print("\nWould KEEP (no_match, but names still look related — "
                  "token_sort_ratio understates these, see docstring):")
            for _, mn, inn, ev, alt in name_floor_kept:
                print(f"  name={ev.get('name_similarity')} alt={alt:.3f} | {mn} ~ {inn}")

        if not apply:
            print("\nDry run — nothing changed. Re-run with --apply to execute.")
            return

        for r, _, _, _, _ in stale:
            # Same contract as api/routes/reviews.py::_do_reject — record the
            # known-different pair so it is never re-flagged, then resolve.
            if r.master_id and r.incoming_id:
                db.add(SuppressedMatch(
                    kind="known_different", master_id=r.master_id, other_id=r.incoming_id,
                ))
            r.status = "rejected"
            r.resolved_at = datetime.utcnow()
        db.commit()

        print(f"\nRejected {len(stale)} stale review(s), each with a known_different "
              f"suppression recorded. No startup records were modified.")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Execute (default is a dry run)")
    ap.add_argument("--verbose", action="store_true", help="List every individual decision")
    args = ap.parse_args()
    run(args.apply, args.verbose)


if __name__ == "__main__":
    main()
