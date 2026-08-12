"""
Remediation for a bug in scripts/apply_empty_field_reviews.py (12 Aug 2026).

That script treated every pending field_update review independently, based
on each review's OWN frozen `old` snapshot (accurate at the moment THAT
review was created). Where multiple pending reviews existed for the same
(master, field) — the same true "field is empty" fact rediscovered by
several ingestion runs over time, each proposing its own value — it applied
every one of them in whatever order the DB query happened to return,
silently overwriting the field each time. The last one processed won by
accident of query order, not by any actual quality/recency signal.

Measured: 861 reviews touched, 218 (master, field) pairs written more than
once, 130 of those genuinely disagree (not just a case/whitespace variant)
across 92 distinct startups.

FIX: revert those 130 fields to their true pre-cleanup state (empty), then
restore every review that proposed one of the discarded candidates back to
`status="pending"` with the field back in its proposed_changes — exactly
its state before the buggy script touched it. This makes the EXISTING
GET /reviews/grouped endpoint (Phase Y-1) do the right thing for free: it
already dedupes multiple pending proposals for the same (master, field)
into one set of candidates for a human to pick between via
POST /reviews/grouped/{master_id}/resolve — see that endpoint's own
comment ("siblings hold old snapshots taken at different times... showing
one of them as current would mislead the human"), which already anticipated
exactly this class of problem. No new UI or API needed.

Only touches the 130 confirmed-genuine clashes (identical/cosmetic-only
duplicates, e.g. "Germany" written twice, are left as they are -- reverting
those would just re-flood the queue with a distinction without a
difference).

Dry run by default.

Usage:
    python3 scripts/revert_multi_candidate_clobbers.py
    python3 scripts/revert_multi_candidate_clobbers.py --apply
"""
import argparse
import functools
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal          # noqa: E402
from database.models import DuplicateReview, Startup   # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001


def _find_genuine_clashes(db):
    """Same detection logic used to size this problem originally: group
    every review touched by the buggy cleanup by (master, field), keep only
    groups where the proposed values genuinely differ (not just case/
    whitespace)."""
    rows = db.query(DuplicateReview).filter(
        DuplicateReview.review_type == "field_update",
        DuplicateReview.evidence["auto_filled"].isnot(None),
    ).all()

    by_master_field = defaultdict(list)
    for r in rows:
        filled = (r.evidence or {}).get("auto_filled") or {}
        for field, p in filled.items():
            by_master_field[(str(r.master_id), field)].append((r, p))

    genuine = {}
    for key, entries in by_master_field.items():
        if len(entries) < 2:
            continue
        values = [str(p.get("new")) for _, p in entries]
        if len(set(v.strip().lower() for v in values)) > 1:
            genuine[key] = entries
    return genuine


def main() -> int:
    ap = argparse.ArgumentParser(description="Revert the multi-candidate field clobber from the 12 Aug cleanup bug")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from sqlalchemy.orm.attributes import flag_modified
    from api.routes.reviews import _reindex

    db = SessionLocal()
    try:
        clashes = _find_genuine_clashes(db)
        affected_masters = {mid for mid, _ in clashes}
        print(f"genuinely-conflicting (master, field) pairs: {len(clashes)}")
        print(f"distinct masters affected: {len(affected_masters)}")

        reviews_restored = set()
        masters_reverted = set()

        for (mid, field), entries in clashes.items():
            master = db.query(Startup).filter(Startup.id == mid).first()
            if master is None:
                print(f"  SKIP {mid}/{field}: master no longer exists")
                continue

            live_value = getattr(master, field, None)
            print(f"  {master.name!r} . {field}: live={live_value!r} <- "
                  f"reverting to empty ({len(entries)} candidates restored to pending)")

            if args.apply:
                setattr(master, field, None)
                masters_reverted.add(mid)

            for r, p in entries:
                proposed = dict(r.proposed_changes or {})
                proposed[field] = {k: v for k, v in p.items()}  # restore full original proposal
                evidence = dict(r.evidence or {})
                auto_filled = dict(evidence.get("auto_filled") or {})
                auto_filled.pop(field, None)
                if auto_filled:
                    evidence["auto_filled"] = auto_filled
                else:
                    evidence.pop("auto_filled", None)
                    evidence.pop("auto_filled_at", None)
                evidence.pop("resolution", None)

                if args.apply:
                    r.proposed_changes = proposed
                    r.evidence = evidence
                    flag_modified(r, "proposed_changes")
                    flag_modified(r, "evidence")
                    if r.status == "approved":
                        r.status = "pending"
                        r.resolved_at = None
                    db.commit()
                reviews_restored.add(str(r.id))

        if args.apply:
            for mid in masters_reverted:
                master = db.query(Startup).filter(Startup.id == mid).first()
                if master is not None:
                    _reindex(db, master)

        print(f"\n  masters reverted        : {len(masters_reverted) if args.apply else len(affected_masters)}")
        print(f"  reviews restored/reopened: {len(reviews_restored)}")
        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
