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
  POST   /reviews/bulk-approve    approve N reviews in one call (Phase Q4)
  POST   /reviews/bulk-reject     reject N reviews in one call (Phase Q4)
"""
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from fastapi import Depends

from database.connection import get_db
from database.models import Startup, DuplicateReview, SuppressedMatch
from processing.storage import _sanitize_for_column

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
        # Phase Q1/Q3: this is the SINGLE reindex path every update flows
        # through (edits, mark-interest, reclassify, recheck, web-verify) —
        # keep semantic-search results (rendered straight from this Qdrant
        # payload) as accurate as Browse's SQL reads.
        "business_model": master.business_model,
        "is_gmbh": master.is_gmbh,
        "interest_status": master.interest_status,
    })


def _apply_field_updates(db, master: Startup, proposed: dict) -> None:
    """
    Apply an approved field_update diff to the master. Never lets one
    malformed field abort the whole approve — skips it (logged) and keeps
    applying the rest, since the alternative (a 500 on approve) blocks a
    human from applying every OTHER, perfectly good field in the same
    review too.
    """
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
            ok, cleaned = _sanitize_for_column(field, new_val)
            if not ok:
                logger.warning(
                    f"[Reviews] Skipping field '{field}' on approve for "
                    f"'{master.name}': value doesn't fit the column ({new_val!r})"
                )
                continue
            setattr(master, field, cleaned)
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
    q: Optional[str] = None,       # company name filter — matches master_name or incoming_name
    run_id: Optional[str] = None,  # Phase Q2: filter to one bulk-verify/recheck batch's results
    evidence_level: Optional[str] = None,  # Phase J-2 (4 Aug): "minimal" | "normal" — see storage._create_review
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """List reviews (pending by default). The dashboard Review Inbox reads this."""
    from sqlalchemy import or_

    query = db.query(DuplicateReview)
    if status:
        query = query.filter(DuplicateReview.status == status)
    if review_type:
        query = query.filter(DuplicateReview.review_type == review_type)
    if risk_level:
        query = query.filter(DuplicateReview.risk_level == risk_level)
    if run_id:
        query = query.filter(DuplicateReview.run_id == run_id)
    if evidence_level:
        query = query.filter(DuplicateReview.evidence["evidence_level"].astext == evidence_level)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            DuplicateReview.master_name.ilike(like),
            DuplicateReview.incoming_name.ilike(like),
        ))
    # `total` must be the real count of everything matching the filters, not
    # len(rows) — that was capped by `limit` (200 from the sidebar badge poll,
    # app.js), so once the true pending count passed 200 the badge/KPI froze
    # at 200 forever regardless of how many reviews got resolved (found live
    # 29 Jul — the "pending count never changes" report, true count was 594).
    total = query.count()
    rows = query.order_by(DuplicateReview.created_at.desc()).limit(limit).all()
    return {
        "total": total,
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
                "run_id": r.run_id,
                "evidence_level": (r.evidence or {}).get("evidence_level"),
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


def _do_approve(db, r: DuplicateReview) -> dict:
    """
    Shared by the single-item and bulk approve endpoints.
    field_update       → apply the proposed changes to the master.
    duplicate/anomaly  → merge the incoming row into the master (one canonical id).
    Raises HTTPException on invalid state — caller decides whether that
    aborts the whole request (single) or is caught per-item (bulk).
    """
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


@router.post("/{review_id}/approve")
async def approve_review(review_id: str, db: Session = Depends(get_db)):
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    return _do_approve(db, r)


@router.post("/{review_id}/undo-merge")
async def undo_merge(review_id: str, db: Session = Depends(get_db)):
    """
    Reverse an approved possible_duplicate/anomaly merge: reinsert the
    deleted "incoming" row from the review's own incoming_data snapshot
    (kept even after approval, specifically for this) under its original
    deterministic id, re-embed it to Qdrant, then flip the review back to
    rejected + record a known-different suppression so it won't be
    re-flagged the next time this source is re-ingested.

    The master it was merged into is left untouched — _fill_empty_fields
    only ever fills fields that were blank, so the merge could not have
    overwritten already-populated master data; there's nothing to revert
    there. This mirrors the manual recovery done live 29 Jul for an
    accidental Swiss Founders Fund / BLP Digital merge.

    Best-effort, disclosed limits: the original source_url/source_name
    aren't preserved in incoming_data (never were), so the restored row's
    provenance says "restored via undo" rather than the true original
    source; source_excerpt is only recoverable for merges that happened
    after this endpoint shipped (earlier snapshots never captured it) —
    older restores come back "unverified, no excerpt on file", same as any
    pre-H-1 legacy record.
    """
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    if r.review_type not in ("possible_duplicate", "anomaly"):
        raise HTTPException(status_code=400, detail="Only a possible-duplicate/anomaly merge can be undone")
    if r.status != "approved":
        raise HTTPException(status_code=409, detail=f"Review is {r.status}, not approved — nothing to undo")
    if not r.incoming_id or not r.incoming_data:
        raise HTTPException(status_code=422, detail="This review has no recoverable incoming-record data")

    if db.query(Startup).filter(Startup.id == r.incoming_id).first() is not None:
        raise HTTPException(
            status_code=409,
            detail="The incoming record still exists — approving this review didn't merge/delete anything, so there's nothing to undo",
        )
    master = db.query(Startup).filter(Startup.id == r.master_id).first()
    if master is None:
        raise HTTPException(status_code=410, detail="The record this was merged into no longer exists — can't safely undo")

    from processing.deduplicator import extract_domain, generate_fingerprint, name_to_stable_uuid
    from processing.storage import _insert_master, _score_and_index
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store

    startup = dict(r.incoming_data)
    name = (startup.get("name") or r.incoming_name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Incoming data has no name — can't reinsert")

    website = startup.get("website") or ""
    domain = extract_domain(website)
    fingerprint = generate_fingerprint(name, website) if domain else None
    stable_id = name_to_stable_uuid(name, website)
    if not stable_id:
        raise HTTPException(status_code=422, detail="Could not derive an identity for the incoming record")
    if db.query(Startup).filter(Startup.id == stable_id).first() is not None:
        raise HTTPException(status_code=409, detail="A record with this identity already exists — can't safely reinsert")
    if str(stable_id) != str(r.incoming_id):
        logger.warning(
            f"[Reviews] Undo-merge id mismatch for '{name}': incoming_data now "
            f"derives {stable_id}, review recorded {r.incoming_id} — reinserting "
            f"under the freshly-derived id."
        )

    now = datetime.utcnow()
    source = r.source or "manual"
    source_entry = {
        "source": source,
        "source_name": None,
        "url": "",
        "date": now.isoformat(),
        "extracted_at": r.created_at.isoformat() if r.created_at else now.isoformat(),
        "run_id": r.run_id,
        "note": "Restored via Undo merge — original source_url wasn't preserved in the review snapshot",
    }
    published_date = startup.get("published_date")

    incoming_vector = embedder.embed(embedder.build_startup_text(startup))
    row = _insert_master(db, startup, name, website, fingerprint, stable_id,
                          source, "", source_entry, published_date, now)
    row.created_at = r.created_at or now
    row.extracted_at = r.created_at or now
    db.commit()
    _score_and_index(row, startup, incoming_vector, fingerprint, source, "",
                      published_date, db, qdrant_store, flag_modified)

    db.add(SuppressedMatch(kind="known_different", master_id=r.master_id, other_id=row.id))
    r.status = "rejected"
    r.resolved_at = datetime.utcnow()
    db.commit()

    return {
        "status": "undone",
        "restored_id": str(row.id),
        "restored_name": name,
        "review_status": r.status,
    }


def _do_reject(db, r: DuplicateReview) -> dict:
    """
    Shared by the single-item and bulk reject endpoints. Discard and
    remember the decision so the same thing is not re-flagged:
      field_update       → suppress each (master_id, field, rejected value)
      duplicate/anomaly  → record the (master_id, incoming_id) known-different pair
    """
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


@router.post("/{review_id}/reject")
async def reject_review(review_id: str, db: Session = Depends(get_db)):
    r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    return _do_reject(db, r)


class BulkReviewRequest(BaseModel):
    ids: List[str]


@router.post("/bulk-approve")
async def bulk_approve_reviews(request: BulkReviewRequest, db: Session = Depends(get_db)):
    """
    Approve a human-selected set of reviews in one call (Phase Q4, 29 Jul —
    built after the queue hit 1,010 pending; a bulk-select toolbar in the
    Review Inbox calls this). Still 100% human-triggered — nothing here
    auto-approves anything the human didn't explicitly select; this is a
    click-count reduction, not a change to the "human approves everything"
    rule. One bad id never aborts the rest: each review is approved
    independently and its own failure is reported, not raised.
    """
    if not request.ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    approved, failed = [], []
    for review_id in request.ids:
        r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
        if not r:
            failed.append({"id": review_id, "error": "not found"})
            continue
        try:
            _do_approve(db, r)
            approved.append(review_id)
        except HTTPException as exc:
            db.rollback()
            failed.append({"id": review_id, "error": exc.detail})
        except Exception as exc:
            db.rollback()
            logger.error(f"[Reviews] bulk-approve failed for {review_id}: {exc}")
            failed.append({"id": review_id, "error": str(exc)})

    return {"approved": len(approved), "failed": failed, "total": len(request.ids)}


@router.post("/bulk-reject")
async def bulk_reject_reviews(request: BulkReviewRequest, db: Session = Depends(get_db)):
    """Same shape as bulk-approve, for reject. See its docstring."""
    if not request.ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    rejected, failed = [], []
    for review_id in request.ids:
        r = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
        if not r:
            failed.append({"id": review_id, "error": "not found"})
            continue
        try:
            _do_reject(db, r)
            rejected.append(review_id)
        except HTTPException as exc:
            db.rollback()
            failed.append({"id": review_id, "error": exc.detail})
        except Exception as exc:
            db.rollback()
            logger.error(f"[Reviews] bulk-reject failed for {review_id}: {exc}")
            failed.append({"id": review_id, "error": str(exc)})

    return {"rejected": len(rejected), "failed": failed, "total": len(request.ids)}


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
