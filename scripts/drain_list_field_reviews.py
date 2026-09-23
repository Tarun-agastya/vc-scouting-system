"""
Clear the pending list-field reviews by merging them. Dry run by default.

Why these are not decisions
---------------------------
Measured on the live queue, 23 Sep 2026:

    569 pending field_update reviews
      462  tags
       55  founders
      105  sub_industry        <- genuinely contested, NOT touched here
       ~45 city/website/stage  <- need research, NOT touched here

Of the 517 tags + founders reviews, **every one was lossless**:

      295  the field was EMPTY — nothing to lose
      222  the new list was a SUPERSET of the old — pure addition
        0  would have dropped a value
        0  genuine conflicts

Not one of them was a question. A person clicking approve 517 times was
performing a set union by hand, and every sweep refilled the queue because
nothing merged the lists automatically.

`field_policy.merge_list_field` now does it at write time, so these stop being
created. This script clears the backlog that already exists.

What it will not do
-------------------
Only `tags` and `founders`, and only when the merge is **lossless** — the
union must contain everything the master already had. Anything that would drop
a value is left pending, by construction, even though the measurement says no
such review currently exists. The guarantee should hold for data that arrives
tomorrow, not just for data measured today.

`sub_industry` is deliberately excluded. All 105 of those are real
disagreements — "Payments" -> "HealthTech", "Supplements - Health & Wellness"
-> "Supplements" — where one side is simply wrong. That is judgement, and it
belongs to a person or to the adjudicator, not to a merge rule.

Hashtags are stripped in passing: 158 tags arrived from newsletters as
"#Finanzierung", "#Instagram", "#Moverloop". The word is worth keeping, the
hash is formatting from another medium and makes "#Biotech" and "Biotech" two
different tags.

Usage
-----
    python3 scripts/drain_list_field_reviews.py
    python3 scripts/drain_list_field_reviews.py --apply
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from sqlalchemy.orm.attributes import flag_modified

from database.connection import SessionLocal
from database.models import DuplicateReview, Startup
from processing.field_policy import merge_list_field, safe_string_list

LIST_FIELDS = {"tags", "founders"}


def _proposed(review) -> dict:
    prop = review.proposed_changes or {}
    if isinstance(prop, str):
        try:
            prop = json.loads(prop)
        except Exception:
            return {}
    return prop or {}


def run(apply: bool) -> None:
    db = SessionLocal()
    try:
        pending = (db.query(DuplicateReview)
                   .filter(DuplicateReview.status == "pending",
                           DuplicateReview.review_type == "field_update")
                   .all())

        mergeable, skipped = [], Counter()
        for r in pending:
            prop = _proposed(r)
            fields = set(prop)
            if not fields or not fields <= LIST_FIELDS:
                for f in fields - LIST_FIELDS:
                    skipped[f] += 1
                continue

            master = db.query(Startup).filter(Startup.id == r.master_id).first()
            if master is None:
                skipped["master deleted"] += 1
                continue

            plan = {}
            lossless = True
            for field, change in prop.items():
                current = getattr(master, field, None) if field != "founders" else \
                    (master.raw_data or {}).get("founders")
                merged = merge_list_field(current, change.get("new"))
                if merged is None:
                    continue                      # nothing new; review is moot
                have = {str(x).lstrip("#").strip().casefold()
                        for x in safe_string_list(current)}
                got = {str(x).strip().casefold() for x in merged}
                if not have <= got:               # the guarantee, re-checked per review
                    lossless = False
                    break
                plan[field] = merged
            if not lossless:
                skipped["would lose a value"] += 1
                continue
            mergeable.append((r, master, plan))

        print(f"Pending field_update reviews : {len(pending)}")
        print(f"  mergeable without loss     : {len(mergeable)}")
        print(f"  left for a human           : {len(pending) - len(mergeable)}")
        if skipped:
            print("\n  why the rest stay pending:")
            for k, v in skipped.most_common():
                print(f"      {k:24} {v}")

        print("\n  examples of what would merge:")
        for r, master, plan in mergeable[:5]:
            for f, merged in plan.items():
                before = safe_string_list(getattr(master, f, None) if f != "founders"
                                          else (master.raw_data or {}).get("founders"))
                print(f"      {(master.name or '?')[:22]:24} {f:9} "
                      f"{len(before)} -> {len(merged)}  {merged[:4]}")

        if not apply:
            print("\nDry run — nothing applied. Re-run with --apply.")
            return

        applied = 0
        for r, master, plan in mergeable:
            for field, merged in plan.items():
                if field == "founders":
                    raw = dict(master.raw_data or {})
                    raw["founders"] = merged
                    master.raw_data = raw
                    flag_modified(master, "raw_data")
                else:
                    setattr(master, field, merged)
            master.updated_at = datetime.utcnow()
            r.status = "approved"
            r.resolved_at = datetime.utcnow()
            applied += 1
        db.commit()

        left = (db.query(DuplicateReview)
                .filter(DuplicateReview.status == "pending").count())
        print(f"\nMerged and closed {applied} reviews.")
        print(f"Still pending overall: {left}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually merge (default: dry run)")
    args = ap.parse_args()
    run(args.apply)


if __name__ == "__main__":
    main()
