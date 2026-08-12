"""
Retroactively apply the empty-field-fill policy (processing/web_verifier.py::
_split_proposal, 12 Aug 2026) to the EXISTING pending field_update review
backlog, not just new web-verification runs going forward.

WHY. The Review Inbox had 1,739 pending field_update rows, many of them
uncontested fills (an empty field, a sourced value proposed for it) rather
than real disagreements — the exact flooding pattern the code change fixed
for new runs. This is the one-time cleanup for what already accumulated,
across every source (startup_network re-sightings, web_verification,
newsletter, RSS, etc.) — not scoped to web_verification alone, per the
owner's explicit choice (12 Aug) after seeing the dry-run split by scope.

For each pending field_update review: split proposed_changes into fills
(old value empty — nothing to adjudicate) and conflicts (a real, non-empty
value being contradicted). Fills are applied directly to the master via the
SAME _apply_field_updates() the manual Approve button uses (so founders/
tags/type-sanitization all behave identically to a human clicking Approve,
not a parallel reimplementation), then the master is re-scored/re-embedded
via _reindex() exactly as an approval does. If nothing is left to contest,
the review is marked approved and closed. If a real conflict remains, the
review stays pending but proposed_changes shrinks to just that conflict —
so a human reviewing it later isn't shown a field that's already been
applied. Reviews with NO empty-old fields at all (a pure conflict, or
nothing actually differs) are left completely untouched — this script never
touches possible_duplicate/anomaly reviews either, only field_update.

Dry run by default. Re-embedding ~850 records is real Ollama+Qdrant work
(seconds each) — expect a multi-minute run with --apply, not instant.

Usage:
    python3 scripts/apply_empty_field_reviews.py
    python3 scripts/apply_empty_field_reviews.py --apply
"""
import argparse
import functools
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal        # noqa: E402
from database.models import DuplicateReview, Startup  # noqa: E402
from processing.web_verifier import _split_proposal  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — long run, log file must show live progress


def main() -> int:
    ap = argparse.ArgumentParser(description="Retroactively apply empty-field fills to the pending review backlog")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from sqlalchemy.orm.attributes import flag_modified
    from api.routes.reviews import _apply_field_updates, _reindex

    db = SessionLocal()
    try:
        reviews = (db.query(DuplicateReview)
                   .filter(DuplicateReview.review_type == "field_update",
                           DuplicateReview.status == "pending")
                   .all())
        print(f"pending field_update reviews: {len(reviews)}")

        closed = shrunk = untouched = missing_master = 0
        total_fields_applied = 0

        for i, r in enumerate(reviews, 1):
            fills, conflicts = _split_proposal(r.proposed_changes or {})
            if not fills:
                untouched += 1
                continue

            master = db.query(Startup).filter(Startup.id == r.master_id).first()
            if not master:
                missing_master += 1
                continue

            total_fields_applied += len(fills)

            ev = dict(r.evidence or {})
            ev["auto_filled"] = fills
            ev["auto_filled_at"] = datetime.utcnow().isoformat()

            if args.apply:
                _apply_field_updates(db, master, fills)
                _reindex(db, master)  # commits master's score/embedding changes

            if conflicts:
                shrunk += 1
                if args.apply:
                    r.proposed_changes = conflicts
                    r.evidence = ev
                    flag_modified(r, "evidence")
                    db.commit()
            else:
                closed += 1
                if args.apply:
                    r.status = "approved"
                    r.resolved_at = datetime.utcnow()
                    ev["resolution"] = "auto_approved_empty_field_fill"
                    r.evidence = ev
                    flag_modified(r, "evidence")
                    db.commit()

            if i % 50 == 0:
                print(f"  processed {i}/{len(reviews)}  "
                      f"(closed {closed}, shrunk {shrunk}, untouched {untouched})")

        print(f"\n  fully resolved (closed)          {closed}")
        print(f"  partially resolved (shrunk)      {shrunk}")
        print(f"  untouched (no empty fields)      {untouched}")
        if missing_master:
            print(f"  skipped (master no longer exists) {missing_master}")
        print(f"  total individual fields applied  {total_fields_applied}")

        if args.apply:
            remaining = (db.query(DuplicateReview)
                        .filter(DuplicateReview.review_type == "field_update",
                                DuplicateReview.status == "pending").count())
            print(f"\n  ✓ applied. Remaining pending field_update reviews: {remaining}")
        else:
            print(f"\n  DRY RUN — nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
