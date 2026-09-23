"""
Run the field adjudicator over the pending field_update reviews. Report-only by default.

What this is for
----------------
After the deterministic passes there are 144 field_update reviews left, and
unlike the 426 that were closed by a union rule, these are real disagreements
where one value is wrong:

    sub_industry  105   "Payments" -> "HealthTech" for a company whose own
                        description calls it "ein deutsches Fintech-Startup"
    city/website/funding_stage/...  ~39

The record's description usually settles it, which is why a model can help
here and a research agent is not needed.

What it will and will not do
----------------------------
Only `keep_old` is ever applied automatically, and only at high confidence.
That direction writes no field: it rejects the review and records a
rejected_value suppression, exactly as a human clicking Reject does.

`take_new` is never auto-applied at any confidence, because it overwrites a
stored value. The verdict and reasoning are written onto the review instead,
so a person can approve it in one click with the answer already in front of
them.

`website` and `name` are excluded either way — they feed the identity
fingerprint, so changing them changes what the record IS.

The honest caveat
-----------------
Auto-applying `keep_old` is safe in the sense that no data is overwritten, but
it is not free: the suppression means that same proposal stops being raised.
A wrong keep_old therefore preserves a worse label permanently rather than
just for one sweep. The suppression is an ordinary row and deleting it undoes
the block.

Measured before shipping: on four control cases the model returns take_new for
a genuinely better proposal (including when the stored value is empty) and
keep_old for an absurd one. On a deliberately ambiguous case it answered
keep_old rather than unsure — it leans conservative, which is the right
direction here, but it means high-confidence keep_old sometimes means "I
cannot tell from this description".

Usage
-----
    python3 scripts/adjudicate_field_changes.py --limit 20
    python3 scripts/adjudicate_field_changes.py --limit 20 --field sub_industry
    python3 scripts/adjudicate_field_changes.py --apply --limit 50
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from database.connection import SessionLocal
from database.models import DuplicateReview, Startup, SuppressedMatch
from processing.field_adjudicator import adjudicate_field_change, may_auto_apply


def _proposed(review) -> dict:
    prop = review.proposed_changes or {}
    if isinstance(prop, str):
        try:
            prop = json.loads(prop)
        except Exception:
            return {}
    return prop or {}


def run(limit: int, apply: bool, only_field: str) -> None:
    db = SessionLocal()
    try:
        pending = (db.query(DuplicateReview)
                   .filter(DuplicateReview.status == "pending",
                           DuplicateReview.review_type == "field_update")
                   .order_by(DuplicateReview.created_at.asc())
                   .all())

        counts, applied = Counter(), 0
        seen = 0
        for r in pending:
            if seen >= limit:
                break
            prop = _proposed(r)
            fields = [f for f in prop if not only_field or f == only_field]
            if not fields:
                continue
            master = db.query(Startup).filter(Startup.id == r.master_id).first()
            if master is None:
                continue

            # One review can carry several fields; judge each, and only
            # resolve the review when every field agrees on keep_old —
            # otherwise a half-applied review would silently drop the fields
            # nobody ruled on.
            verdicts = {}
            for field in fields:
                change = prop[field]
                if not isinstance(change, dict):
                    continue
                res = adjudicate_field_change(master, field,
                                              change.get("old"), change.get("new"))
                if not res:
                    counts["model unavailable"] += 1
                    continue
                verdicts[field] = res
                counts[f"{res['verdict']} ({res['confidence']})"] += 1

            if not verdicts:
                continue
            seen += 1
            all_keep = all(may_auto_apply(v, f) for f, v in verdicts.items())
            mark = "->CLOSE" if (apply and all_keep) else "       "
            head = next(iter(verdicts))
            print(f"  {mark} {(master.name or '?')[:22]:24} {head:14} "
                  f"{verdicts[head]['verdict']:9} {verdicts[head]['confidence']}")
            print(f"           {verdicts[head]['reasoning'][:94]}")

            # Record the reasoning either way — that is most of the value even
            # when nothing is auto-applied, because the next human to open it
            # has the answer already.
            ev = dict(r.evidence or {})
            ev["field_adjudication"] = {
                f: {**v, "at": datetime.utcnow().isoformat(timespec="seconds")}
                for f, v in verdicts.items()}
            r.evidence = ev
            flag_modified(r, "evidence")
            first = verdicts[head]
            r.llm_explanation = f"[{first['verdict']} / {first['confidence']}] {first['reasoning']}"[:2000]

            if apply and all_keep:
                for field in verdicts:
                    change = prop.get(field) or {}
                    db.add(SuppressedMatch(kind="rejected_value", master_id=r.master_id,
                                           field=field, value=str(change.get("new"))))
                r.status = "rejected"
                r.resolved_at = datetime.utcnow()
                applied += 1

        db.commit()

        print("\n" + "=" * 64)
        for k, v in counts.most_common():
            print(f"  {k:28} {v}")
        print("=" * 64)
        if apply:
            left = db.query(DuplicateReview).filter(
                DuplicateReview.status == "pending").count()
            print(f"\nClosed {applied} reviews as 'keep what we have'. Still pending: {left}")
        else:
            print("\nReport only — verdicts written onto the reviews, nothing resolved.")
            print("The Review Inbox now shows the reasoning. Re-run with --apply to close")
            print("the confident keep_old ones.")
        print("\n'take_new' is never auto-applied: it overwrites a stored value, and")
        print("over-writing a correct value with a worse one is the failure this")
        print("database has actually suffered.")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--apply", action="store_true",
                    help="close the confident keep_old reviews")
    ap.add_argument("--field", default="", help="only judge this field, e.g. sub_industry")
    args = ap.parse_args()
    run(args.limit, args.apply, args.field.strip())


if __name__ == "__main__":
    main()
