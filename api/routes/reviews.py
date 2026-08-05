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

def _filtered_reviews(db, *, status, review_type, risk_level, q, run_id, evidence_level):
    """
    The shared Review-Inbox filter chain. Factored out (Phase Y-1, 5 Aug) so
    GET /reviews and GET /reviews/grouped can never drift apart in what they
    consider "matching" — the grouped view is the same queue, just collapsed
    to one entry per startup.
    """
    from sqlalchemy import or_, cast
    from sqlalchemy.dialects.postgresql import JSONB

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
        # DuplicateReview.evidence is a plain JSON column (not JSONB), and
        # only JSONB's comparator exposes .astext — the same 500 this
        # codebase already hit twice on Startup.verification_evidence
        # (api/routes/verification.py, processing/web_verifier.py). Same
        # fix: cast to JSONB at query time, no column migration needed.
        query = query.filter(
            cast(DuplicateReview.evidence, JSONB)["evidence_level"].astext == evidence_level
        )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            DuplicateReview.master_name.ilike(like),
            DuplicateReview.incoming_name.ilike(like),
        ))
    return query


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
    query = _filtered_reviews(
        db, status=status, review_type=review_type, risk_level=risk_level,
        q=q, run_id=run_id, evidence_level=evidence_level,
    )
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


# NOTE: this route MUST stay declared BEFORE @router.get("/{review_id}") —
# FastAPI matches in declaration order, so a later /grouped would be swallowed
# by the path-param route and 404 as "review 'grouped' not found".
@router.get("/grouped")
async def list_reviews_grouped(
    status: str = Query("pending"),
    risk_level: Optional[str] = None,
    q: Optional[str] = None,
    run_id: Optional[str] = None,
    evidence_level: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """
    The Review Inbox collapsed to ONE ENTRY PER STARTUP (Phase Y-1, 5 Aug).

    Measured on the live queue that day: 1,090 pending field_update reviews
    covering only 355 distinct startups — 223 startups appeared more than
    once (Bliro 11×, Omegga/Alqem 18×), because each ingestion run re-crawling
    the same page stages its own review with slightly reworded LLM output.
    A human triaging that sees the same company over and over.

    Grouping is DISPLAY-ONLY: no review row is merged, deleted or mutated
    here, so the per-run audit trail (and each row's run_id) stays intact —
    the S-3b contract. The underlying rows remain individually resolvable
    through the existing endpoints.

    Only `field_update` reviews group. A possible_duplicate/anomaly is a
    genuine distinct pair and is deliberately left alone.
    """
    from collections import defaultdict

    query = _filtered_reviews(
        db, status=status, review_type="field_update", risk_level=risk_level,
        q=q, run_id=run_id, evidence_level=evidence_level,
    )
    rows = query.order_by(DuplicateReview.created_at.desc()).all()

    by_master = defaultdict(list)
    for r in rows:
        by_master[str(r.master_id)].append(r)

    # One query for every master in the page, not one per group.
    master_ids = list(by_master.keys())
    masters = {}
    if master_ids:
        for m in db.query(Startup).filter(Startup.id.in_(master_ids)).all():
            masters[str(m.id)] = m

    groups = []
    for mid, members in by_master.items():
        master = masters.get(mid)
        # Candidates per field, deduped by exact value. Of 2,986 field
        # proposals live, 470 were exact duplicates — pure noise this
        # collapses for free, while `count`/`review_ids` keep it auditable.
        fields = defaultdict(dict)
        for r in members:                      # already newest-first
            for field, change in (r.proposed_changes or {}).items():
                value = change.get("new")
                key = str(value)
                cand = fields[field].get(key)
                if cand is None:
                    fields[field][key] = {
                        "value": value,
                        "count": 1,
                        "review_ids": [str(r.id)],
                        "sources": [{
                            "source": change.get("incoming_source"),
                            "at": change.get("incoming_extracted_at"),
                        }],
                    }
                else:
                    cand["count"] += 1
                    cand["review_ids"].append(str(r.id))
                    cand["sources"].append({
                        "source": change.get("incoming_source"),
                        "at": change.get("incoming_extracted_at"),
                    })

        # `current` comes from the LIVE master row, never from any review's
        # stored `old`. Siblings hold `old` snapshots taken at different
        # times, so once grouped they disagree with each other and with
        # reality — showing one of them as "current" would mislead the human
        # into approving against a value that no longer exists.
        current = {}
        for field in fields:
            if master is None:
                current[field] = None
            elif field == "founders":
                current[field] = (master.raw_data or {}).get("founders")
            else:
                current[field] = getattr(master, field, None)

        created = [r.created_at for r in members if r.created_at]
        groups.append({
            "master_id": mid,
            "master_name": members[0].master_name,
            "review_ids": [str(r.id) for r in members],
            "review_count": len(members),
            # Worst risk across the group — a single high-risk overwrite
            # buried among low-risk blank-fills must not read as low.
            "risk_level": "high" if any(r.risk_level == "high" for r in members) else members[0].risk_level,
            "first_seen": min(created) if created else None,
            "last_seen": max(created) if created else None,
            "fields": {f: list(cands.values()) for f, cands in fields.items()},
            "current": current,
            "master_missing": master is None,
        })

    groups.sort(key=lambda g: g["last_seen"] or "", reverse=True)
    return {
        "total_groups": len(groups),
        "total_reviews": len(rows),
        "groups": groups[:limit],
    }


class GroupResolveRequest(BaseModel):
    selections: dict  # {field: chosen_value}; a field simply absent = reject all its candidates


@router.post("/grouped/{master_id}/resolve")
async def resolve_grouped_reviews(master_id: str, request: GroupResolveRequest, db: Session = Depends(get_db)):
    """
    Apply one pick per field across ALL of a startup's pending field_update
    reviews, in one action (Phase Y-2, 5 Aug) — the other half of GET
    /reviews/grouped. Reuses _apply_field_updates/_reindex/_do_reject
    UNCHANGED, so approving through the grouped view behaves identically to
    approving one review, just aggregated: one write to the master, one
    _reindex (not one per constituent review — Bliro's 11 pending reviews
    used to cost 11 embeds + 11 Qdrant upserts, all but the last thrown
    away), and every constituent review still individually resolved with
    its own audit trail (approved if its value was picked, rejected +
    suppressed otherwise) rather than merged away.
    """
    master = db.query(Startup).filter(Startup.id == master_id).first()
    if not master:
        raise HTTPException(status_code=404, detail="Startup not found")

    members = (
        db.query(DuplicateReview)
        .filter(
            DuplicateReview.master_id == master_id,
            DuplicateReview.review_type == "field_update",
            DuplicateReview.status == "pending",
        )
        .all()
    )
    if not members:
        raise HTTPException(status_code=404, detail="No pending field_update reviews for this startup")

    selections = request.selections or {}

    # Build ONE synthetic proposed_changes dict from the picks and reuse
    # _apply_field_updates exactly as approve_review does — never a second
    # implementation of what "apply a field" means.
    to_apply = {}
    for field, value in selections.items():
        if value is None:
            continue
        source_meta = None
        for r in members:
            change = (r.proposed_changes or {}).get(field)
            if change is not None and change.get("new") == value:
                source_meta = change
                break
        to_apply[field] = {
            "old": getattr(master, field, None),
            "new": value,
            "incoming_source": (source_meta or {}).get("incoming_source"),
            "incoming_extracted_at": (source_meta or {}).get("incoming_extracted_at"),
        }

    if to_apply:
        _apply_field_updates(db, master, to_apply)
        db.commit()
        _reindex(db, master)  # commits internally — the ONE reindex for this whole group

    # Resolve every constituent review. A review is "approved" if EVERY field
    # it proposed matches what was picked for that field (value + review),
    # so a review proposing 3 fields where only 2 were picked correctly
    # shows as rejected on its own un-picked field, not silently approved.
    approved_ids, rejected_ids = [], []
    now = datetime.utcnow()
    for r in members:
        proposed = r.proposed_changes or {}
        member_approved = bool(proposed) and all(
            selections.get(field) == change.get("new") for field, change in proposed.items()
        )
        if member_approved:
            r.status = "approved"
            approved_ids.append(str(r.id))
        else:
            # Same contract as _do_reject: suppress each proposed value so a
            # future re-extraction doesn't re-stage the same rejected wording.
            for field, change in proposed.items():
                db.add(SuppressedMatch(
                    kind="rejected_value", master_id=r.master_id,
                    field=field, value=str(change.get("new")),
                ))
            r.status = "rejected"
            rejected_ids.append(str(r.id))
        r.resolved_at = now

    db.commit()

    logger.info(
        f"[Reviews] Grouped resolve for '{master.name}' ({master_id}): "
        f"{len(approved_ids)} review(s) approved, {len(rejected_ids)} rejected, "
        f"{len(to_apply)} field(s) applied"
    )
    return {
        "status": "resolved",
        "master_id": master_id,
        "applied_fields": list(to_apply.keys()),
        "approved_review_ids": approved_ids,
        "rejected_review_ids": rejected_ids,
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
