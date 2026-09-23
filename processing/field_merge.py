"""
Field-level merge, and the undo that makes it safe to offer.

The old merge (`reviews._merge_records`) could only fill fields that were
EMPTY. That was crude, but it carried an accidental safety property: it was
incapable of destroying anything you already had, so nobody ever needed to
undo one. Letting a human choose per field which value wins removes exactly
that property — replacing a populated value is the whole point.

So undo is not an extra here. It is the thing that makes the feature
shippable, and it is written first: `merge_records` refuses to proceed unless
it has successfully written a snapshot.

What gets restored
------------------
Ids in this database are derived from name+website rather than being random
(see deduplicator.name_to_stable_uuid), so a restored record comes back with
the SAME id it had. Every reference to it still resolves — which is not true
of most systems, and is the reason undo here can be a genuine reversal rather
than a re-import.

Only the keeper fields the merge actually CHANGED are snapshotted, with their
prior values. Storing the whole keeper would make undo destructive in its own
right: it would roll back edits made to unrelated fields after the merge.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Fields a human can choose between. Deliberately the ones with human meaning
# — not scores, timestamps, embedding ids or other machine bookkeeping, which
# are recomputed and would be noise in a merge screen.
MERGEABLE_FIELDS = [
    "name", "website", "short_description", "description",
    "industry", "sub_industry", "tech_cluster", "tags",
    "country", "city", "address", "founded_year", "employee_count",
    "funding_stage", "total_funding_usd", "contact_info", "linkedin",
    "business_model", "is_gmbh",
]

# Never taken from the losing record or offered as a choice: identity,
# provenance and derived values. source_history is unioned separately; the
# scores are recomputed from the merged result.
_NEVER_MERGE = {
    "id", "normalized_name", "fingerprint", "created_at", "updated_at",
    "embedding_id", "enrichment_score", "score_tier", "score_breakdown",
    "source_history", "raw_data",
}


def _val(row, field):
    return getattr(row, field, None)


def build_merge_preview(keeper, loser) -> list:
    """
    One entry per mergeable field, for the merge screen.

    `differs` drives the UI: matching fields are collapsed by default so only
    genuine conflicts need attention. `default` prefers whichever side has a
    value, and the keeper when both do — the conservative choice, since the
    keeper is the older record and the one that survives.
    """
    out = []
    for field in MERGEABLE_FIELDS:
        k, l = _val(keeper, field), _val(loser, field)
        k_empty = k in (None, "", [], {})
        l_empty = l in (None, "", [], {})
        if k_empty and l_empty:
            continue
        out.append({
            "field": field,
            "keeper": k,
            "incoming": l,
            "differs": _norm(k) != _norm(l),
            "default": "incoming" if k_empty else "keeper",
        })
    return out


def _norm(v):
    if isinstance(v, (list, tuple)):
        return sorted(str(x).strip().casefold() for x in v)
    return str(v).strip().casefold() if v is not None else ""


def _row_to_dict(row) -> dict:
    """Whole row as JSON-safe primitives, enough to recreate it exactly."""
    out = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name, None)
        out[col.name] = v.isoformat() if isinstance(v, datetime) else (
            str(v) if col.name.endswith("_id") or col.name == "id" else v
        )
    return out


def merge_records(db, keeper, loser, choices: dict, review_id=None) -> dict:
    """
    Apply a field-level merge and return the snapshot row.

    `choices` maps field -> "keeper" | "incoming". Anything absent keeps the
    keeper's value. Refuses to run without persisting a snapshot first.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from database.models import MergeSnapshot
    from vector_db.qdrant_store import qdrant_store

    keeper_before, applied = {}, {}
    for field, side in (choices or {}).items():
        if field in _NEVER_MERGE or field not in MERGEABLE_FIELDS:
            continue
        if side != "incoming":
            continue
        new_val = _val(loser, field)
        old_val = _val(keeper, field)
        if _norm(new_val) == _norm(old_val):
            continue
        keeper_before[field] = old_val
        applied[field] = new_val

    snapshot = MergeSnapshot(
        keeper_id=keeper.id, loser_id=loser.id,
        keeper_name=keeper.name, loser_name=loser.name,
        loser_row=_row_to_dict(loser),
        keeper_before={k: (v if not isinstance(v, datetime) else v.isoformat())
                       for k, v in keeper_before.items()},
        choices=dict(choices or {}),
        review_id=review_id,
    )
    db.add(snapshot)
    db.flush()          # fail here, before anything is destroyed

    from processing.change_log import changes_from

    with changes_from("merge", detail=f"merged from '{loser.name}'"):
        for field, value in applied.items():
            setattr(keeper, field, value)
            if isinstance(value, (list, dict)):
                flag_modified(keeper, field)
        # Flush INSIDE the block. The change-log hook runs at flush time and
        # reads the attribution contextvar then — a commit after the block has
        # exited records the change correctly but labels it "system", which is
        # worse than useless in a timeline whose whole purpose is saying what
        # caused something.
        db.flush()

    hist = list(keeper.source_history or [])
    known = {e.get("url") for e in hist}
    for e in (loser.source_history or []):
        if e.get("url") not in known:
            hist.append(e)
            known.add(e.get("url"))
    keeper.source_history = hist
    flag_modified(keeper, "source_history")
    keeper.updated_at = datetime.utcnow()

    try:
        qdrant_store.delete_startup(str(loser.id))
    except Exception as exc:
        logger.warning(f"[Merge] Qdrant delete failed for {loser.id}: {exc}")
    db.delete(loser)
    db.commit()

    logger.info(f"[Merge] '{snapshot.loser_name}' into '{snapshot.keeper_name}' — "
                f"{len(applied)} field(s) taken from the merged record")
    return {"snapshot_id": str(snapshot.id), "fields_changed": sorted(applied),
            "keeper_id": str(keeper.id)}


def undo_merge(db, snapshot_id) -> dict:
    """
    Recreate the deleted record and roll back the keeper's changed fields.

    Restores only the fields this merge touched, so an unrelated edit made
    afterwards survives.
    """
    from database.models import MergeSnapshot, Startup
    from sqlalchemy.orm.attributes import flag_modified

    snap = db.query(MergeSnapshot).filter(MergeSnapshot.id == snapshot_id).first()
    if snap is None:
        return {"status": "not_found"}
    if snap.undone_at:
        return {"status": "already_undone", "undone_at": snap.undone_at.isoformat()}

    if db.query(Startup).filter(Startup.id == snap.loser_id).first():
        return {"status": "conflict",
                "detail": "A record with the merged id exists again — undoing would collide."}

    row = dict(snap.loser_row or {})
    cols = {c.name: c for c in Startup.__table__.columns}
    payload = {}
    for k, v in row.items():
        if k not in cols:
            continue
        if isinstance(v, str) and str(cols[k].type).startswith("DATETIME"):
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                v = None
        payload[k] = v
    db.add(Startup(**payload))

    from processing.change_log import changes_from

    keeper = db.query(Startup).filter(Startup.id == snap.keeper_id).first()
    reverted = []
    if keeper is not None:
        with changes_from("undo", detail=f"undo of merge with '{snap.loser_name}'"):
            for field, old in (snap.keeper_before or {}).items():
                setattr(keeper, field, old)
                if isinstance(old, (list, dict)):
                    flag_modified(keeper, field)
                reverted.append(field)
            keeper.updated_at = datetime.utcnow()
            db.flush()          # inside the block — see merge_records

    snap.undone_at = datetime.utcnow()
    db.commit()

    # Re-index both records; embedding is cheap and a stale vector is worse
    # than a missing one.
    try:
        from processing.storage import _rescore
        for row_obj in (keeper, db.query(Startup).filter(Startup.id == snap.loser_id).first()):
            if row_obj is not None:
                _rescore(row_obj, row_obj.source_url or "", db, flag_modified)
        db.commit()
    except Exception as exc:
        logger.warning(f"[Merge] re-index after undo failed: {exc}")

    logger.info(f"[Merge] UNDONE — restored '{snap.loser_name}', "
                f"reverted {len(reverted)} field(s) on '{snap.keeper_name}'")
    return {"status": "undone", "restored": snap.loser_name,
            "reverted_fields": reverted, "keeper": snap.keeper_name}


def prune_snapshots(db, days: int = 30) -> int:
    """Drop snapshots past the undo window. Returns how many went."""
    from database.models import MergeSnapshot
    cutoff = datetime.utcnow() - timedelta(days=days)
    n = db.query(MergeSnapshot).filter(MergeSnapshot.created_at < cutoff).delete(
        synchronize_session=False)
    db.commit()
    return n
