"""
One-time retroactive drain of the pending field_update backlog against the
Phase Z policy (12 Aug 2026) — processing/field_policy.py's better_freetext/
norm_value, now live for new reviews since commit 0645c45, but not applied
to what had already accumulated before that shipped.

WHY THIS EXISTS SEPARATELY FROM apply_empty_field_reviews.py (11 Aug). That
script handled empty-field fills only. This one additionally resolves
description/short_description deterministically (they never used to leave
the review queue at all) and folds locale-variant candidates (Munich/
München) together before counting distinct values — both new as of Phase Z.

STRUCTURED PER-MASTER, NOT PER-REVIEW — the lesson from the 12 Aug cleanup
bug (see scripts/revert_multi_candidate_clobbers.py's post-mortem): judging
each review against its own frozen `old` snapshot in isolation is what let
130 fields across 92 startups get silently overwritten by whichever review
a DB query happened to return last. Every field this script ever writes is
decided by first collecting EVERY pending candidate for that (master, field)
across ALL its reviews, THEN deciding — never one review at a time.

Per master, per field:
  - TEXT (short_description/description): fold every candidate plus the
    current live value through better_freetext to find the overall winner.
    If it differs from the live value, apply it. Every review that proposed
    this field is considered resolved for it either way (a text field never
    stays "pending" — see field_policy.py's docstring for why it left the
    review queue entirely).
  - NON-TEXT: dedupe candidates by norm_value.
      * live value populated -> genuine conflict, left exactly as-is
        (pending, unresolved) for a human via the normal grouped picker.
      * live value empty, exactly ONE distinct candidate -> applied
        directly (an uncontested fill).
      * live value empty, 2+ distinct candidates -> left pending
        (multi-candidate guard — a real ambiguity, not this script's call).

A review closes (status="approved") only once EVERY field it proposed has
been resolved one way or another; otherwise it stays pending with
proposed_changes narrowed to whatever's left. Reuses _apply_field_updates
and _reindex (api/routes/reviews.py) — the exact same functions the Approve
button and every other resolution path in this codebase already use.

Dry run by default.

Usage:
    python3 scripts/drain_review_backlog.py
    python3 scripts/drain_review_backlog.py --apply
"""
import argparse
import functools
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal          # noqa: E402
from database.models import DuplicateReview, Startup   # noqa: E402
from processing.field_policy import better_freetext, norm_value  # noqa: E402
from processing.storage import _TEXT_FIELDS            # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001

_EMPTY = (None, "", 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Retroactively drain the pending field_update backlog against Phase Z policy")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from sqlalchemy.orm.attributes import flag_modified
    from api.routes.reviews import _apply_field_updates, _reindex

    db = SessionLocal()
    try:
        rows = (db.query(DuplicateReview)
                .filter(DuplicateReview.review_type == "field_update",
                        DuplicateReview.status == "pending")
                .all())
        print(f"pending field_update reviews: {len(rows)}")

        by_master = defaultdict(list)
        for r in rows:
            by_master[str(r.master_id)].append(r)
        print(f"distinct masters: {len(by_master)}")

        closed = shrunk = untouched = missing_master = 0
        text_applied = nontext_applied = 0

        for mid, members in by_master.items():
            master = db.query(Startup).filter(Startup.id == mid).first()
            if not master:
                missing_master += 1
                continue

            # Collect every candidate per field across every pending review
            # for this master BEFORE deciding anything.
            field_candidates = defaultdict(list)  # field -> [(review, raw_new), ...]
            for r in members:
                for field, change in (r.proposed_changes or {}).items():
                    field_candidates[field].append((r, change.get("new")))

            resolved_fields = {}   # field -> True if this field is now settled (applied or dropped)
            to_apply = {}          # field -> value, for _apply_field_updates

            for field, entries in field_candidates.items():
                if field in _TEXT_FIELDS:
                    current = getattr(master, field, None)
                    best = current
                    for _, candidate in entries:
                        winner = better_freetext(best, candidate)
                        if winner is not None:
                            best = winner
                    resolved_fields[field] = True
                    current_s = "" if current is None else str(current).strip()
                    if best and best != current_s:
                        to_apply[field] = best
                        text_applied += 1
                    continue

                distinct = {}
                for _, candidate in entries:
                    distinct.setdefault(norm_value(candidate), candidate)
                live = getattr(master, field, None)
                if live not in _EMPTY:
                    continue  # genuine conflict — leave exactly as-is
                if len(distinct) == 1:
                    (value,) = distinct.values()
                    to_apply[field] = value
                    resolved_fields[field] = True
                    nontext_applied += 1
                # else: multiple live candidates for a still-empty field —
                # a real ambiguity, leave every one of them pending.

            print(f"  {master.name!r}: applying {list(to_apply.keys())}"
                  if to_apply else f"  {master.name!r}: nothing to apply")

            if args.apply and to_apply:
                _apply_field_updates(db, master, {
                    f: {"new": v} for f, v in to_apply.items()
                })
                db.commit()
                _reindex(db, master)

            now = datetime.utcnow()
            for r in members:
                remaining = {f: c for f, c in (r.proposed_changes or {}).items()
                            if f not in resolved_fields}
                if remaining:
                    shrunk += 1
                    if args.apply and remaining != (r.proposed_changes or {}):
                        r.proposed_changes = remaining
                        flag_modified(r, "proposed_changes")
                        db.commit()
                else:
                    closed += 1
                    if args.apply:
                        r.status = "approved"
                        r.resolved_at = now
                        ev = dict(r.evidence or {})
                        ev["resolution"] = "phase_z_backlog_drain"
                        r.evidence = ev
                        flag_modified(r, "evidence")
                        db.commit()

        print(f"\n  reviews fully resolved (closed)   {closed}")
        print(f"  reviews narrowed (still pending)  {shrunk}")
        if missing_master:
            print(f"  skipped (master no longer exists) {missing_master}")
        print(f"  text fields applied                {text_applied}")
        print(f"  non-text fields applied            {nontext_applied}")

        if args.apply:
            remaining_total = (db.query(DuplicateReview)
                               .filter(DuplicateReview.review_type == "field_update",
                                       DuplicateReview.status == "pending").count())
            print(f"\n  ✓ applied. Remaining pending field_update reviews: {remaining_total}")
        else:
            print(f"\n  DRY RUN — nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
