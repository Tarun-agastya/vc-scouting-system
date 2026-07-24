"""
Data-stewardship Review Inbox API (Phase S-3b).

Surfaces the staged reviews (field updates, possible duplicates, anomalies)
produced by the matcher/storage, and lets a human approve or reject them.
Nothing in the master DB changes except through an explicit approve here.

  GET    /reviews                 list pending (or filtered) reviews
  GET    /reviews/{id}            full side-by-side detail
  POST   /reviews/{id}/approve    field_update -> apply diff to master;
                                  duplicate/anomaly -> merge the two rows
  POST   /reviews/{id}/reject     discard + record suppression (no re-flagging)
  POST   /reviews/{id}/delete     permanently remove the master and/or incoming
                                  record — for "neither merge nor keep, just
                                  remove this data" (e.g. an out-of-scope
                                  company that should never have been stored)
"""
import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from fastapi import Depends

from database.connection import get_db
from database.models import Startup, DuplicateReview, SuppressedMatch

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_startup_dict(row: Startup) -> dict:
    """Flatten a Startup row into the dict shape the embedder/Qdrant expect."""
    raw = row.raw_data or {}
    return {
        "name": row.name,
        "one_liner": row.short_description,
        "description": row.description,
        "website": row.website,
        "industry": row.industry,
        "sub_industry": row.sub_industry,
        "tech_cluster": row.tech_cluster,
        "country": row.country,
        "city": row.city,
        "address": row.address,
        "funding_stage": row.funding_stage,
        "founded_year": row.founded_year,
        "employee_count": row.employee_count,
        "contact_info": row.contact_info,
        "founders": raw.get("founders") or [],
        "tags": row.tags or [],
    }


def _reindex(db, master: Startup) -> None:
    """Re-score and re-embed a master after its data changed (approved update/merge)."""
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from processing.scorer import compute_enrichment_score

    r = compute_enrichment_score(master)
    master.enrichment_score  = r.enrichment_score
    master.source_confidence = r.source_confidence
    master.score_tier        = r.score_tier
    master.score_breakdown   = r.score_breakdown
    master.last_enriched_at  = datetime.utcnow()
    flag_modified(master, "score_breakdown")
    db.commit()

    sd = _row_to_startup_dict(master)
    vec = embedder.embed(embedder.build_startup_text(sd))
    qdrant_store.upsert_startup(str(master.id), vec, {
        **sd,
        "id": str(master.id),
        "fingerprint": master.fingerprint,
        "source": master.source,
        "source_url": master.source_url,
        "extracted_at": master.extracted_at.isoformat() if master.extracted_at else None,
        "enrichment_score": master.enrichment_score or 0.0,
        "source_confidence": master.source_confidence or 0.0,
        "score_tier": master.score_tier or "WEAK_SIGNAL",
        "verification_status": master.verification_status or "unverified",
    })


def _apply_field_updates(db, master: Startup, proposed: dict) -> None:
    """Apply an approved field_update diff to the master."""
    for field, change in (proposed or {}).items():
        new_val = change.get("new")
        if field == "founders":
            raw = dict(master.raw_data or {})
            raw["founders"] = new_val
            master.raw_data = raw
            flag_modified(master, "raw_data")
        elif field == "tags":
            master.tags = new_val
        else:
            setattr(master, field, new_val)
    master.extracted_at = datetime.utcnow()
    master.updated_at = datetime.utcnow()


def _merge_records(db, keeper: Startup, loser: Startup, incoming_data: dict) -> None:
    """Merge loser into keeper (fill blanks + union history), delete loser."""
    from processing.storage import _fill_empty_fields
    from vector_db.qdrant_store import qdrant_store

    _fill_empty_fields(keeper, incoming_data or (loser.raw_data or {}))

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
        logger.warning(f"[Reviews] Qdrant delete failed for loser {loser.id}: {exc}")
    db.delete(loser)
    db.commit()


def _delete_startup_row(db, startup_id, keep_review_id) -> Optional[dict]:
    """
    Permanently remove one Startup row: its Qdrant point, any OTHER pending
    review referencing it (that review's subject just vanished, so it's
    moot — not left dangling on a record that no longer exists), and the
    row itself. `keep_review_id` is excluded from the moot-review cleanup —
    the caller resolves that one itself. Returns {"id", "name"} or None if
    the row was already gone.
    """
    from vector_db.qdrant_store import qdrant_store

    row = db.query(Startup).filter(Startup.id == startup_id).first()
    if row is None:
        return None
    name = row.name

    db.query(DuplicateReview).filter(
        (DuplicateReview.master_id == startup_id) | (DuplicateReview.incoming_id == startup_id),
        DuplicateReview.id != keep_review_id,
        DuplicateReview.status == "pending",
    ).delete(synchronize_session=False)
    db.query(SuppressedMatch).filter(
        (SuppressedMatch.master_id == startup_id) | (SuppressedMatch.other_id == startup_id)
    ).delete(synchronize_session=False)

    db.delete(row)
    try:
        qdrant_store.delete_startup(str(startup_id))
    except Exception as exc:
        logger.warning(f"[Reviews] Qdrant delete failed for {startup_id}: {exc}")

    return {"id": str(startup_id), "name": name}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_reviews(
    status: str = Query("pending"),
    review_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """List reviews (pending by default). The dashboard Review Inbox reads this."""
    q = db.query(DuplicateReview)
    if status:
        q = q.filter(DuplicateReview.status == status)
    if review_type:
        q = q.filter(DuplicateReview.review_type == review_type)
    if risk_level:
        q = q.filter(DuplicateReview.risk_level == risk_level)
    rows = q.order_by(DuplicateReview.created_at.desc()).limit(limit).all()
    return {
        "total": len(rows),
        "reviews": [
            {
                "id": str(r.id),
                "review_type": r.review_type,
                "risk_level": r.risk_level,
                "master_id": str(r.master_id) if r.master_id else None,
                "master_name": r.master_name,
                "incoming_name": r.incoming_name,
                "changed_fields": list((r.proposed_changes or {}).keys()),
                "confidence": r.confidence,
                "source": r.source,
                "llm_explanation": r.llm_explanation,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.get("/{review_id}")
async def get_review(review_id: str, db: Session = Depends(get_db)):
    """Full side-by-side detail for one review."""
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    master = db.query(Startup).filter(Startup.id == r.master_id).first() if r.master_id else None
    return {
        "id": str(r.id),
        "review_type": r.review_type,
        "risk_level": r.risk_level,
        "status": r.status,
        "confidence": r.confidence,
        "evidence": r.evidence,
        "llm_explanation": r.llm_explanation,
        "source": r.source,
        "run_id": r.run_id,
        "created_at": r.created_at,
        "master": _row_to_startup_dict(master) if master else None,
        "master_id": str(r.master_id) if r.master_id else None,
        "incoming": r.incoming_data,
        "incoming_id": str(r.incoming_id) if r.incoming_id else None,
        "proposed_changes": r.proposed_changes,
    }


@router.post("/{review_id}/approve")
async def approve_review(review_id: str, db: Session = Depends(get_db)):
    """
    field_update       → apply the proposed changes to the master.
    duplicate/anomaly  → merge the incoming row into the master (one canonical id).
    """
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    if r.status != "pending":
        raise HTTPException(status_code=409, detail=f"Review already {r.status}")

    master = db.query(Startup).filter(Startup.id == r.master_id).first()
    if not master:
        raise HTTPException(status_code=410, detail="Master record no longer exists")

    if r.review_type == "field_update":
        _apply_field_updates(db, master, r.proposed_changes)
        _reindex(db, master)
        result = {"applied_fields": list((r.proposed_changes or {}).keys())}
    else:  # possible_duplicate | anomaly
        loser = db.query(Startup).filter(Startup.id == r.incoming_id).first()
        if loser and str(loser.id) != str(master.id):
            _merge_records(db, master, loser, r.incoming_data)
            _reindex(db, master)
            result = {"merged_into": str(master.id), "deleted": str(r.incoming_id)}
        else:
            result = {"note": "incoming row missing or same as master — nothing to merge"}

    r.status = "approved"
    r.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "approved", "review_type": r.review_type, **result}


@router.post("/{review_id}/reject")
async def reject_review(review_id: str, db: Session = Depends(get_db)):
    """
    Discard and remember the decision so the same thing is not re-flagged:
      field_update       → suppress each (master_id, field, rejected value)
      duplicate/anomaly  → record the (master_id, incoming_id) known-different pair
    """
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    if r.status != "pending":
        raise HTTPException(status_code=409, detail=f"Review already {r.status}")

    if r.review_type == "field_update":
        for field, change in (r.proposed_changes or {}).items():
            db.add(SuppressedMatch(
                kind="rejected_value", master_id=r.master_id,
                field=field, value=str(change.get("new")),
            ))
    else:
        if r.master_id and r.incoming_id:
            db.add(SuppressedMatch(
                kind="known_different", master_id=r.master_id, other_id=r.incoming_id,
            ))

    r.status = "rejected"
    r.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "rejected", "review_type": r.review_type}


@router.post("/{review_id}/delete")
async def delete_review_data(
    review_id: str,
    target: str = Query(..., description='"incoming" | "master" | "both"'),
    db: Session = Depends(get_db),
):
    """
    Permanently remove the master and/or incoming record tied to this
    review — the third outcome besides approve (merge) and reject (keep
    both, remember they're different): sometimes a reviewer wants neither —
    the data itself is wrong or out of scope (e.g. a non-European company
    an extraction pulled in by mistake) and should just be gone.

    target="incoming": delete only the incoming record (the common case —
      an out-of-scope/bad extraction flagged against an otherwise-fine
      master). Only valid for possible_duplicate/anomaly, which have a
      separate incoming row; field_update has none (incoming_id is always
      NULL there — see DuplicateReview's docstring).
    target="master": delete only the master.
    target="both": delete both records.

    Any OTHER pending review whose subject just got deleted is cleaned up
    too (it would otherwise dangle on a vanished record). This review is
    marked "deleted" — distinct from "rejected", so the audit trail is
    honest about what actually happened.
    """
    if target not in ("incoming", "master", "both"):
        raise HTTPException(status_code=422, detail='target must be "incoming", "master", or "both"')

    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    if r.status != "pending":
        raise HTTPException(status_code=409, detail=f"Review already {r.status}")

    if target in ("incoming", "both") and not r.incoming_id:
        raise HTTPException(
            status_code=422,
            detail="This review has no separate incoming record to delete "
                   "(field_update reviews only have a master) — use target=master",
        )

    deleted = []
    if target in ("incoming", "both"):
        d = _delete_startup_row(db, r.incoming_id, keep_review_id=review_id)
        if d:
            deleted.append({"role": "incoming", **d})
    if target in ("master", "both") and r.master_id:
        d = _delete_startup_row(db, r.master_id, keep_review_id=review_id)
        if d:
            deleted.append({"role": "master", **d})

    if not deleted:
        raise HTTPException(status_code=410, detail="Nothing left to delete — record(s) already gone")

    r.status = "deleted"
    r.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "deleted", "review_type": r.review_type, "deleted": deleted}
