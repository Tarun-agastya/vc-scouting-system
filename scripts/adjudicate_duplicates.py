"""
Run the LLM adjudicator over the duplicate pairs no rule could settle.

Report-only by default. Even with --apply it will never merge anything: the
only automatic action is recording a `known_different` pair, which is
reversible by deleting one row. See processing/dedup_adjudicator.py for why
that asymmetry exists.

What it works on
----------------
Pending `possible_duplicate` reviews. After the 22 Sep deterministic cleanup
these are the genuinely ambiguous ones — the volume cases were already handled
without a model, taking 3,591 records to 2,899.

Every verdict is written onto the review (`llm_explanation`, plus a structured
copy under `evidence["adjudication"]`) so it shows up in the Review Inbox next
to the evidence a human is already reading. That happens in report mode too:
seeing the reasoning is useful whether or not anything is auto-applied.

Usage
-----
    python3 scripts/adjudicate_duplicates.py --limit 20        # report only
    python3 scripts/adjudicate_duplicates.py --limit 20 --apply
    python3 scripts/adjudicate_duplicates.py --apply --yes     # no prompt
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from processing.dedup_adjudicator import adjudicate_pair, may_auto_apply


def _both_records(db, review):
    master = db.query(Startup).filter(Startup.id == review.master_id).first()
    incoming = None
    if review.incoming_id:
        incoming = db.query(Startup).filter(Startup.id == review.incoming_id).first()
    if incoming is None:
        # The incoming side may only exist as the snapshot on the review.
        incoming = review.incoming_data or {}
    return master, incoming


def run(limit: int, apply: bool, assume_yes: bool) -> None:
    db = SessionLocal()
    try:
        reviews = (
            db.query(DuplicateReview)
            .filter(DuplicateReview.status == "pending",
                    DuplicateReview.review_type == "possible_duplicate")
            .order_by(DuplicateReview.created_at.asc())
            .limit(limit).all()
        )
        print(f"Pending duplicate reviews to adjudicate: {len(reviews)}")
        if apply and not assume_yes:
            print("\n--apply will record known-different pairs for confident "
                  "'different_company' verdicts.\nNothing is merged or deleted. "
                  "Ctrl-C now to stop.\n")

        counts = {"same_company": 0, "different_company": 0,
                  "insufficient_evidence": 0, "unavailable": 0}
        applied = 0

        for r in reviews:
            master, incoming = _both_records(db, r)
            if master is None:
                continue
            result = adjudicate_pair(master, incoming)
            if not result:
                counts["unavailable"] += 1
                print(f"  {(r.master_name or '?')[:28]:30} (model unavailable)")
                continue

            counts[result["verdict"]] += 1
            mark = "->APPLY" if (apply and may_auto_apply(result)) else "       "
            print(f"  {mark} {(r.master_name or '?')[:26]:28} "
                  f"{result['verdict']:22} {result['confidence']:7} "
                  f"via {result['key_signal'][:30]}")

            # Record the verdict either way — the reasoning is the point.
            ev = dict(r.evidence or {})
            ev["adjudication"] = {**result, "at": datetime.utcnow().isoformat(timespec="seconds")}
            r.evidence = ev
            flag_modified(r, "evidence")
            r.llm_explanation = (
                f"[{result['verdict']} / {result['confidence']}] "
                f"{result['reasoning']}"
            )[:2000]

            if apply and may_auto_apply(result) and r.master_id and r.incoming_id:
                db.add(SuppressedMatch(kind="known_different",
                                       master_id=r.master_id, other_id=r.incoming_id))
                r.status = "rejected"
                r.resolved_at = datetime.utcnow()
                applied += 1

        db.commit()

        print("\n" + "=" * 62)
        for k, v in counts.items():
            print(f"  {k:24} {v}")
        print("=" * 62)
        if apply:
            print(f"\nAuto-resolved {applied} pairs as known-different (reversible).")
            print("Everything else stays pending, now with the reasoning attached.")
        else:
            print("\nReport only — verdicts written to the reviews, nothing resolved.")
            print("The Review Inbox will show the reasoning. Re-run with --apply to act")
            print("on confident 'different_company' verdicts.")
        print("\n'same_company' is never auto-merged at any confidence: merging deletes a")
        print("record and cannot be undone, and over-merging has been this database's")
        print("expensive mistake, not under-merging.")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--apply", action="store_true",
                    help="record known-different pairs for confident verdicts")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation notice")
    args = ap.parse_args()
    run(args.limit, args.apply, args.yes)


if __name__ == "__main__":
    main()
