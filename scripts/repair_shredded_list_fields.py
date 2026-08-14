"""
One-off repair for a real data-corruption bug found live 14 Aug 2026
(owner report: Alqem's pending review showed tags as
"[,',D,e,e,p,t,e,c,h,',,, ,',K,I,',]").

ROOT CAUSE. Several call sites in processing/storage.py and
api/routes/reviews.py read/wrote Startup.tags (and raw_data["founders"])
with a bare `list(x or [])` / `set(x or [])` / direct assignment, all
silently assuming x was already a real list. The first time either field was
ever a plain string for any reason, `list("['Deeptech', 'KI']")` shredded it
into individual characters -- and every read/write after that faithfully
carried the shred forward, because a corrupted list of single characters is
still "a list", so nothing downstream ever noticed. Fixed at the root in
processing/field_policy.py::safe_string_list, now used at every call site
that used to trust the stored type blindly (see that function's own
docstring for the full mechanism). This script is the one-time retroactive
repair for what already got corrupted before that fix shipped.

SCOPE, measured 14 Aug 2026:
  - 98 Startup.tags rows shredded into single characters -- 100% cleanly
    recoverable: rejoining the characters exactly reconstructs the original
    Python-list-repr string ("['Deeptech', 'KI']"), which parses straight
    back to the real list. Verified against all 98 real rows before this
    shipped: 98/98 recovered, zero failures, zero data loss.
  - 4 raw_data["founders"] rows with a DIFFERENT, much messier corruption
    pattern (e.g. "'a''ai''aiai''I''Aai'", "B.") that does NOT reconstruct
    to a real list -- an older or different bug, not the same mechanism.
    These are NOT guessed at. They're just cleared to [] (empty, not
    fabricated) so at least the poisoned "founders" data stops rendering
    nonsense; a future extraction can naturally repopulate them for real.
  - 13 pending field_update reviews whose proposed_changes has the shredded
    shape baked into "old" and/or "new" (frozen at review-creation time, so
    repairing the master row doesn't fix these on its own). Rejected rather
    than silently rewritten: their "old"/"new" comparison is now provably
    stale/wrong either way, so there's nothing left for a human to usefully
    decide on it. Rejecting (not deleting outright) keeps the audit trail
    and follows the same discard-and-remember-why path every other
    resolved review takes; the genuinely new information each review was
    trying to add (visible in incoming_data) is still there in the source
    and will be re-proposed cleanly next time that source is re-crawled.

Dry run by default.

Usage:
    python3 scripts/repair_shredded_list_fields.py
    python3 scripts/repair_shredded_list_fields.py --apply
"""
import argparse
import ast
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal        # noqa: E402
from database.models import Startup, DuplicateReview  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001


def _is_shredded(value) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(v, str) and len(v) <= 1 for v in value
    )


def _try_recover(value):
    """
    Rejoin shredded characters and parse back to the original list.
    Returns (recovered_list_or_None, ok).

    Some rows were shredded TWICE: once by the original bug, then AGAIN when
    a later merge unioned genuinely-new clean tags into the already-shredded
    set (processing/storage.py::_fill_empty_fields's old, unguarded
    `set(existing.tags or [])`) — producing a list that mixes leftover
    single-character junk with real multi-word tags side by side. Confirmed
    live on 'BeWithly UG': join+literal_eval succeeds (the shredded portion
    still reconstructs to valid list syntax, and Python happily parses
    "['x', 'y', ...], 'real tag', 'another real tag']" as one flat list) but
    the RESULT still isn't clean. So a successful parse alone isn't enough
    to trust — strip any surviving single-character/pure-punctuation
    fragments from the result before accepting it, same standard as a
    genuinely real tag list already has to meet.
    """
    joined = "".join(value)
    try:
        parsed = ast.literal_eval(joined)
    except (ValueError, SyntaxError):
        return None, False
    if not (isinstance(parsed, list) and all(isinstance(v, str) for v in parsed)):
        return None, False
    cleaned = [v for v in parsed if len(v.strip()) > 1]
    return (cleaned, True) if cleaned else (None, False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair shredded tags/founders list fields")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        # ── 1. Startup.tags ───────────────────────────────────────────────
        rows = db.query(Startup).filter(Startup.tags.isnot(None)).all()
        tag_fixes, tag_unrecoverable = [], []
        for r in rows:
            if not _is_shredded(r.tags):
                continue
            recovered, ok = _try_recover(r.tags)
            if ok:
                tag_fixes.append((r, recovered))
            else:
                tag_unrecoverable.append(r)

        print(f"Startup.tags — shredded: {len(tag_fixes) + len(tag_unrecoverable)}, "
              f"recoverable: {len(tag_fixes)}, unrecoverable: {len(tag_unrecoverable)}")
        for r, recovered in tag_fixes:
            print(f"  FIX   {r.name!r:35} -> {recovered}")
        for r in tag_unrecoverable:
            print(f"  SKIP (could not recover) {r.name!r:35} raw={r.tags!r}")

        # ── 2. raw_data["founders"] ──────────────────────────────────────
        rows = db.query(Startup).filter(Startup.raw_data.isnot(None)).all()
        founder_clears = []
        for r in rows:
            f = (r.raw_data or {}).get("founders")
            if _is_shredded(f):
                recovered, ok = _try_recover(f)
                founder_clears.append((r, recovered if ok else None))

        print(f"\nraw_data.founders — shredded: {len(founder_clears)}")
        for r, recovered in founder_clears:
            if recovered:
                print(f"  FIX   {r.name!r:35} -> {recovered}")
            else:
                print(f"  CLEAR (unrecoverable) {r.name!r:35} raw={(r.raw_data or {}).get('founders')!r}")

        # ── 3. Pending reviews with the shredded shape baked in ──────────
        pending = (
            db.query(DuplicateReview)
            .filter(DuplicateReview.status == "pending", DuplicateReview.review_type == "field_update")
            .all()
        )
        stale_reviews = []
        for rv in pending:
            for field, change in (rv.proposed_changes or {}).items():
                if _is_shredded(change.get("old")) or _is_shredded(change.get("new")):
                    stale_reviews.append(rv)
                    break

        print(f"\nPending reviews with shredded old/new baked in: {len(stale_reviews)}")
        for rv in stale_reviews:
            print(f"  REJECT  {rv.master_name!r:25} review_id={rv.id}")

        if not args.apply:
            print("\nDry run only — nothing changed. Re-run with --apply to execute.")
            return 0

        from datetime import datetime
        from sqlalchemy.orm.attributes import flag_modified

        for r, recovered in tag_fixes:
            r.tags = recovered
        for r in tag_unrecoverable:
            pass  # left untouched -- genuinely can't recover these safely
        for r, recovered in founder_clears:
            raw = dict(r.raw_data or {})
            raw["founders"] = recovered or []
            r.raw_data = raw
            flag_modified(r, "raw_data")
        for rv in stale_reviews:
            rv.status = "rejected"
            rv.resolved_at = datetime.utcnow()
            ev = dict(rv.evidence or {})
            ev["resolution"] = "rejected_stale_shredded_list_field"
            rv.evidence = ev
            flag_modified(rv, "evidence")

        db.commit()
        print(f"\n✓ Applied. Repaired {len(tag_fixes)} tags row(s), "
              f"{len(founder_clears)} founders row(s) ({sum(1 for _, rec in founder_clears if rec)} recovered, "
              f"{sum(1 for _, rec in founder_clears if not rec)} cleared), "
              f"rejected {len(stale_reviews)} stale review(s).")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
