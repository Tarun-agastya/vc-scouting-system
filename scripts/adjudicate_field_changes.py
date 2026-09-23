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
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from database.connection import SessionLocal
from database.models import DuplicateReview, Startup, SuppressedMatch
from processing.field_adjudicator import adjudicate_field_group, group_may_auto_apply


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

        # Group by (record, field) BEFORE judging anything. Several reviews
        # frequently propose different values for the same field — 30 of the
        # 65 companies with a sub_industry review had more than one — and
        # judging them one at a time produced contradictory verdicts for the
        # same company. Grouping is also cheaper: one call instead of three.
        groups = defaultdict(list)
        for r in pending:
            prop = _proposed(r)
            for field, change in prop.items():
                if only_field and field != only_field:
                    continue
                if isinstance(change, dict):
                    groups[(r.master_id, field)].append((r, change))

        counts, applied, suppressed = Counter(), 0, 0
        for i, ((master_id, field), items) in enumerate(groups.items()):
            if i >= limit:
                break
            master = db.query(Startup).filter(Startup.id == master_id).first()
            if master is None:
                continue

            current = getattr(master, field, None)
            candidates = [c.get("new") for _r, c in items]
            res = adjudicate_field_group(master, field, current, candidates)
            if not res:
                counts["model unavailable"] += 1
                continue

            keeps = group_may_auto_apply(res, field)
            counts[("keep current" if res["winner"] is None else "prefers a proposal")
                   + f" ({res['confidence']})"] += 1
            mark = "->CLOSE" if (apply and keeps) else "       "
            n = f"[{len(items)} proposals]" if len(items) > 1 else ""
            print(f"  {mark} {(master.name or '?')[:22]:24} {field:13} {n}")
            print(f"           stored: {str(current)[:58]}")
            for c in res["considered"]:
                flag = "  <- picked" if c == res["winner"] else ""
                print(f"           cand  : {str(c)[:58]}{flag}")
            if res["winner"] is None:
                print(f"           keeps the stored value")
            print(f"           {res['reasoning'][:92]}")

            for r, _c in items:
                ev = dict(r.evidence or {})
                ev["field_adjudication"] = {
                    **res, "at": datetime.utcnow().isoformat(timespec="seconds")}
                r.evidence = ev
                flag_modified(r, "evidence")
                r.llm_explanation = (
                    f"[{'keep stored' if res['winner'] is None else 'prefers: ' + str(res['winner'])}"
                    f" / {res['confidence']}] {res['reasoning']}")[:2000]

            if apply and keeps:
                for r, change in items:
                    db.add(SuppressedMatch(kind="rejected_value", master_id=master_id,
                                           field=field, value=str(change.get("new"))))
                    r.status = "rejected"
                    r.resolved_at = datetime.utcnow()
                    suppressed += 1
                applied += 1

        db.commit()

        print("\n" + "=" * 64)
        for k, v in counts.most_common():
            print(f"  {k:34} {v}")
        print("=" * 64)
        if apply:
            left = db.query(DuplicateReview).filter(
                DuplicateReview.status == "pending").count()
            print(f"\nClosed {suppressed} reviews across {applied} records. Still pending: {left}")
        else:
            print("\nReport only — reasoning written onto every review, nothing resolved.")
            print("Re-run with --apply to close the ones where the stored value wins.")
        print("\nA proposal that WINS is never applied automatically: it overwrites a")
        print("stored value, and over-writing a correct value with a worse one is the")
        print("failure this database has actually suffered. Those stay for a person,")
        print("with the model's pick and reasoning already attached.")
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
