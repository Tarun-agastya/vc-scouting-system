"""
Split the lossless list part out of MIXED field_update reviews (A3).

drain_list_field_reviews.py only touches a review whose EVERY proposed field
is tags/founders. A review that proposes, say, city AND tags together is
skipped whole — so its tags union (which is lossless by construction, see
field_policy.merge_list_field) sits in the queue forever next to a real
question. Measured 29 Sep: 34 of 125 pending field_update rows.

This applies the list part and rewrites the review to carry ONLY the scalar
fields, leaving it pending for exactly the question that needs a person. The
review row is never deleted or closed here — if the list fields were all it
had it is a pure list review and the older script owns it.

Union only: a value is never dropped (merge_list_field returns None when
nothing would change, and the guarantee is re-checked per review).
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

LIST_FIELDS = {"tags", "founders"}


def drain_mixed_list_fields(db, *, apply: bool = False, master_ids=None) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from database.models import DuplicateReview, Startup
    from processing.change_log import changes_from
    from processing.field_policy import merge_list_field, safe_string_list

    q = db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending", DuplicateReview.review_type == "field_update")
    if master_ids is not None:
        q = q.filter(DuplicateReview.master_id.in_(master_ids))

    stats = {"mixed_reviews": 0, "lists_merged": 0}
    for r in q.all():
        prop = dict(r.proposed_changes or {})
        lists = {f: c for f, c in prop.items() if f in LIST_FIELDS and isinstance(c, dict)}
        scalars = set(prop) - LIST_FIELDS
        if not lists or not scalars:
            continue                                # pure list or pure scalar: not ours
        master = db.query(Startup).filter(Startup.id == r.master_id).first()
        if master is None:
            continue
        stats["mixed_reviews"] += 1
        if not apply:
            continue

        with changes_from("review", detail="list fields split from a mixed review"):
            for field, change in lists.items():
                current = (safe_string_list((master.raw_data or {}).get("founders"))
                           if field == "founders" else master.tags)
                merged = merge_list_field(current, change.get("new"))
                if merged is not None:
                    have = {str(x).lstrip("#").strip().casefold() for x in safe_string_list(current)}
                    if not have <= {m.casefold() for m in merged}:
                        continue                    # would lose a value: leave it in the review
                    if field == "founders":
                        raw = dict(master.raw_data or {})
                        raw["founders"] = merged
                        master.raw_data = raw
                        flag_modified(master, "raw_data")
                    else:
                        master.tags = merged
                    stats["lists_merged"] += 1
                prop.pop(field)                     # merged, or already contained: nothing left to ask
            master.updated_at = datetime.utcnow()
            db.flush()
        r.proposed_changes = prop
        flag_modified(r, "proposed_changes")
        db.commit()
    return stats
