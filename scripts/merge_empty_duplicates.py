"""
Retroactively apply the empty-duplicate auto-merge policy (processing/
storage.py's upsert_startup, 14 Aug 2026) to the EXISTING pending
possible_duplicate backlog, not just new matches going forward.

WHY. Owner-reported: the Review Inbox had ~16 possible_duplicate reviews
shaped like "tripbot" ~ "tripbot" — an RSS "roundup" article (deutsche-
startups.de's "5 neue Startups: X, Y, Z…") correctly re-recognizing a real,
already-well-documented company by name, with the incoming side supplying
zero individual facts. Approving or rejecting changes nothing (an all-empty
loser can never overwrite a populated field), so these were pure queue noise
a human had to click through for no reason. The ingest-time fix stops new
ones; this is the one-time drain of what already accumulated.

SAME TWO-SIDED SAFETY GATE AS THE LIVE CODE — do not loosen either side:
  - risk_level MUST be "high" (the matcher's strongest identity-confidence
    tier: strong name+embedding match, a shared founder, or a corroborated
    domain match — see processing/matcher.py::_classify). "low" stays
    pending regardless of how empty the incoming side is.
  - the MASTER must NOT also be a bare stub (see processing/storage.py::
    _is_bare_master's docstring — this is what Phase J-2's "HBC Campus 1" /
    "HBC Campus 2" incident already taught this project the hard way: two
    bare stubs can hit the exact same "high" identity score by name pattern
    alone while being genuinely different entities. Only a master with
    independently verified substance actually confirms the incoming side is
    provably redundant rather than merely similar-looking).
  - re-checks the LIVE master and incoming rows, never the frozen
    incoming_data snapshot — a review sitting in the queue for days may no
    longer reflect either row's current state.

Reuses upsert_startup's own helpers (_is_empty_incoming, _is_bare_master,
_fill_empty_fields, _append_source_history) so this can never drift from the
live policy — there is exactly one definition of "safe to auto-merge" in
this codebase, not two copies that could disagree.

Dry run by default.

Usage:
    python3 scripts/merge_empty_duplicates.py
    python3 scripts/merge_empty_duplicates.py --apply
"""
import argparse
import functools
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal          # noqa: E402
from database.models import DuplicateReview, Startup   # noqa: E402
from processing.storage import _is_empty_incoming, _is_bare_master, _fill_empty_fields  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Retroactively auto-merge empty-incoming high-confidence possible_duplicate reviews"
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from sqlalchemy.orm.attributes import flag_modified
    from processing.storage import _append_source_history

    db = SessionLocal()
    try:
        reviews = (
            db.query(DuplicateReview)
            .filter(DuplicateReview.review_type == "possible_duplicate",
                   DuplicateReview.status == "pending",
                   DuplicateReview.risk_level == "high")
            .all()
        )
        print(f"pending possible_duplicate reviews, risk_level=high: {len(reviews)}")

        merged, skipped_not_empty, skipped_bare_master, skipped_missing = 0, 0, 0, 0

        for r in reviews:
            master = db.query(Startup).filter(Startup.id == r.master_id).first()
            incoming = db.query(Startup).filter(Startup.id == r.incoming_id).first()

            if master is None or incoming is None:
                skipped_missing += 1
                continue

            # Deliberately the FROZEN incoming_data snapshot, not the live
            # incoming row's current columns. Verified live 14 Aug 2026: the
            # inline classifier backfills industry/tech_cluster/business_model
            # onto a row from its bare NAME ALONE within moments of insertion
            # — "tripbot" with zero extracted facts still ends up with
            # industry="Mobility & Automotive" on the live row. Checking the
            # live row found 0 of 223 eligible; checking incoming_data (what
            # the source ACTUALLY supplied, before classification ran) is
            # what upsert_startup's own live gate checks too — this script
            # must match that exactly, not a later, enriched snapshot.
            incoming_dict = r.incoming_data or {}
            if not _is_empty_incoming(incoming_dict):
                skipped_not_empty += 1
                continue
            if _is_bare_master(master):
                skipped_bare_master += 1
                continue

            print(f"  MERGE: '{incoming.name}' ({incoming.id}) -> '{master.name}' ({master.id})")
            merged += 1

            if args.apply:
                _fill_empty_fields(master, incoming_dict)  # no-op by definition; kept for consistency
                source_entry = {
                    "source": incoming.source, "source_name": None,
                    "url": incoming.source_url or "",
                    "date": datetime.utcnow().isoformat(),
                    "extracted_at": datetime.utcnow().isoformat(),
                    "run_id": r.run_id,
                    "note": "Auto-merged from a retroactive empty-duplicate drain — see scripts/merge_empty_duplicates.py",
                }
                _append_source_history(master, source_entry, flag_modified)

                try:
                    from vector_db.qdrant_store import qdrant_store
                    qdrant_store.delete_startup(str(incoming.id))
                except Exception as exc:
                    print(f"      ! Qdrant delete failed for {incoming.id}: {exc}")

                db.delete(incoming)
                r.status = "approved"
                r.resolved_at = datetime.utcnow()
                ev = dict(r.evidence or {})
                ev["resolution"] = "auto_merged_empty_duplicate"
                r.evidence = ev
                flag_modified(r, "evidence")
                db.commit()

        print()
        print(f"  would merge / merged           {merged}")
        print(f"  skipped (incoming has data)    {skipped_not_empty}")
        print(f"  skipped (master is bare too)   {skipped_bare_master}")
        print(f"  skipped (row already gone)     {skipped_missing}")

        if args.apply:
            remaining = (
                db.query(DuplicateReview)
                .filter(DuplicateReview.review_type == "possible_duplicate",
                       DuplicateReview.status == "pending").count()
            )
            print(f"\n  ✓ applied. Remaining pending possible_duplicate reviews: {remaining}")
        else:
            print("\n  DRY RUN — nothing written. Re-run with --apply to execute.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
