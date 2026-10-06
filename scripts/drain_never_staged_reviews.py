"""
Close the pending reviews that should never have been staged: fields that are
no longer staged at all (sub_industry — processing/storage.py::
_NEVER_STAGED_FIELDS) and changes that are not changes
(field_policy.is_noise_change: same website domain, one value containing the
other). Dry-run by default.

  * a review proposing ONLY such fields is closed as rejected, WITHOUT a
    suppression row: the policy already keeps the stored value, and a
    suppression would wrongly imply a person judged that value bad;
  * a review that also proposes other fields loses just that key and stays
    pending for the rest.

Every row touched is marked in evidence (`policy_closed`) so it is never
mistaken for a person's decision — the trust ledger skips it.

    python3 scripts/drain_never_staged_reviews.py            # dry run
    python3 scripts/drain_never_staged_reviews.py --apply
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from database.connection import SessionLocal
from database.models import DuplicateReview
from database.models import Startup
from processing.field_policy import is_noise_change
from processing.storage import _NEVER_STAGED_FIELDS


def run(apply: bool, master_ids=None) -> dict:
    db = SessionLocal()
    stats = {"closed": 0, "stripped": 0}
    try:
        q = db.query(DuplicateReview).filter(
            DuplicateReview.status == "pending", DuplicateReview.review_type == "field_update")
        if master_ids is not None:
            q = q.filter(DuplicateReview.master_id.in_(master_ids))
        for r in q.all():
            prop = dict(r.proposed_changes or {})
            hit = set(prop) & _NEVER_STAGED_FIELDS
            # Also anything that is not a change at all (same website domain,
            # one value containing the other — field_policy.is_noise_change),
            # judged against the record's LIVE value, not the review's frozen
            # snapshot.
            master = db.query(Startup).filter(Startup.id == r.master_id).first()
            if master is not None:
                for f, ch in prop.items():
                    if isinstance(ch, dict) and f not in hit and is_noise_change(
                            f, getattr(master, f, None), ch.get("new")):
                        hit.add(f)
            if not hit:
                continue
            if set(prop) <= hit:
                stats["closed"] += 1
                if apply:
                    ev = dict(r.evidence or {})
                    ev["policy_closed"] = "not staged (policy or no real change): " + ", ".join(sorted(hit))
                    r.evidence = ev
                    flag_modified(r, "evidence")
                    r.status, r.resolved_at = "rejected", datetime.utcnow()
            else:
                stats["stripped"] += 1
                if apply:
                    for f in hit:
                        prop.pop(f)
                    r.proposed_changes = prop
                    flag_modified(r, "proposed_changes")
        if apply:
            db.commit()
    finally:
        db.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(run(a.apply), "(applied)" if a.apply else "(dry run — nothing written)")
