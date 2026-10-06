"""
Measure the field adjudicator against decisions humans ALREADY made, and
(with --apply) record the result in the trust ledger as source="backtest".

Why: autonomy is earned by agreement with people (processing/trust.py), but
the ledger only fills as people work the live queue — which is the very flood
this is meant to end. The database already holds hundreds of settled
single-field reviews (294 on sub_industry alone). Running the model over
those, with the value that was stored AT THE TIME as `current` and the
proposed value as the candidate, is real evidence, available today.

Honesty rules, because a backtest that flatters the model would grant
autonomy it has not earned:
  * the sample is chosen with a FIXED seed, not by picking favourable rows;
  * every disagreement is printed, not just the rate;
  * rows are tagged source="backtest" so the ledger can be recomputed
    without them and nobody mistakes them for live decisions;
  * text fields (description, short_description) are excluded — they are
    resolved by a deterministic length rule, not by this model;
  * identity fields (website, name) are excluded — they can never be earned;
  * only high-confidence verdicts are scored the way the gate scores them,
    but the report shows the full spread so a rate built on few verdicts
    is visible as such.

Known limit: the model sees the record's CURRENT description, which may
differ from what a reviewer saw then. That is noise, not bias, but it is
noise — read the disagreements.

Dry-run by default.
    python3 scripts/backtest_trust.py --fields sub_industry --per-field 60
    python3 scripts/backtest_trust.py --fields sub_industry,funding_stage --apply
"""
import argparse
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import DecisionAudit, DuplicateReview, Startup
from processing.field_adjudicator import IDENTITY_FIELDS, adjudicate_field_group

EXCLUDED = IDENTITY_FIELDS | {"tags", "founders", "description", "short_description"}
SEED = 20260929


def _history(db, fields):
    out = defaultdict(list)
    for r in db.query(DuplicateReview).filter(
            DuplicateReview.review_type == "field_update",
            DuplicateReview.status.in_(["approved", "rejected"])):
        p = r.proposed_changes or {}
        if len(p) != 1:
            continue
        (f, ch), = p.items()
        if f in EXCLUDED or not isinstance(ch, dict) or (fields and f not in fields):
            continue
        if (r.evidence or {}).get("auto_closed_by") or (r.evidence or {}).get("auto_applied"):
            continue                                  # a machine closed it — not a human decision
        out[f].append(r)
    return out


def run(fields, per_field, apply):
    db = SessionLocal()
    try:
        done = {rid for (rid,) in db.query(DecisionAudit.review_id).all()}
        rng = random.Random(SEED)
        for field, reviews in sorted(_history(db, fields).items()):
            reviews = [r for r in reviews if r.id not in done]
            rng.shuffle(reviews)
            reviews = reviews[:per_field]
            tally, wrong, rows = Counter(), [], []
            for r in reviews:
                ch = r.proposed_changes[field]
                master = db.get(Startup, r.master_id)
                if master is None:
                    continue
                res = adjudicate_field_group(master, field, ch.get("old"), [ch.get("new")])
                if not res:
                    tally["model unavailable"] += 1
                    continue
                if res.get("none_fit"):
                    tally["none_fit (no claim)"] += 1
                    continue
                verdict = "keep" if res["winner"] is None else "prefer"
                agreed = (r.status == "rejected") if verdict == "keep" else (r.status == "approved")
                tally[f"{verdict}/{res['confidence']} agreed" if agreed else f"{verdict}/{res['confidence']} DISAGREED"] += 1
                if not agreed and res["confidence"] == "high":
                    wrong.append((master.name, ch.get("old"), ch.get("new"), r.status, res["reasoning"][:110]))
                rows.append(DecisionAudit(
                    review_id=r.id, master_id=r.master_id, field=field, verdict=verdict,
                    confidence=res["confidence"], agreed=agreed,
                    decided_at=r.resolved_at or datetime.utcnow(), source="backtest"))

            hp = [x for x in rows if x.verdict == "prefer" and x.confidence == "high"]
            print(f"\n== {field}: {len(reviews)} sampled ==")
            for k, v in sorted(tally.items()):
                print(f"   {v:4} {k}")
            if hp:
                a = sum(x.agreed for x in hp)
                print(f"   -> gate metric (prefer/high): {a}/{len(hp)} = {a/len(hp):.0%}")
            for name, old, new, status, why in wrong:
                print(f"   DISAGREE  {name[:24]:24} {str(old)[:22]!r} -> {str(new)[:22]!r}  human={status}\n             {why}")
            if apply:
                db.add_all(rows)
                db.commit()
                print(f"   recorded {len(rows)} rows (source=backtest)")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fields", default="sub_industry", help="comma-separated")
    ap.add_argument("--per-field", type=int, default=60)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run({f.strip() for f in a.fields.split(",") if f.strip()}, a.per_field, a.apply)
