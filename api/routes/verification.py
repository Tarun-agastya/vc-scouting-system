"""
Verification/recheck status API (Phase H-3).

  POST /verification/recheck   trigger a recheck batch through the controller
                                (queues on the GPU mutex, returns immediately)
  GET  /verification/status    counts by verification_status, overall and
                                broken down by coarse source type (web/rss/
                                newsletter/manual) — measured, not assumed.
                                Plan addendum 3 (21 Jul 2026): don't let one
                                spot-checked example stand in for the whole
                                picture — this is what actually answers "how
                                much of the DB has bad data, and where."
"""
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from database.connection import get_db
from database.models import Startup

router = APIRouter()
logger = logging.getLogger(__name__)

_STATUSES = ("unverified", "verified", "flagged")


class SelectedIdsRequest(BaseModel):
    ids: List[str]


def _classify_source(raw) -> str:
    """
    Coarse web/rss/newsletter/manual/unknown bucketing for this report
    only — Startup.source itself is left untouched. RSS currently stores
    the feed URL as `source` rather than a clean "rss" label (a pre-existing
    quirk, out of scope for this phase), so a bare URL is classified as
    "rss"; everything else maps by its known source_type label.
    """
    if not raw:
        return "unknown"
    r = str(raw).strip().lower()
    if r == "newsletter":
        return "newsletter"
    if r == "manual":
        return "manual"
    if r.startswith("http://") or r.startswith("https://") or r == "rss":
        return "rss"
    return "web"  # accelerator / incubator / university_hub / startup_network / intelligence_platform / general


@router.post("/recheck")
async def trigger_recheck(limit: int = 20):
    """Queue a verification recheck batch through the controller (GPU mutex)."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_recheck(limit=limit))
    return {"status": "started", "message": "Verification recheck queued via controller"}


@router.post("/web-verify")
async def trigger_web_verify(limit: int = 15):
    """
    Queue a Phase W web-search verification batch through the controller
    (GPU mutex). Manual-trigger only — no nightly scheduler job (owner
    decision, 23 Jul: WebSearch cost/quota profile at scale is unknown).
    """
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_web_verify(limit=limit))
    return {"status": "started", "message": "Web verification batch queued via controller"}


@router.post("/recheck-selected")
async def trigger_recheck_selected(request: SelectedIdsRequest):
    """
    Phase Q2 (29 Jul): recheck an explicit, human-selected set of startups
    from Browse's bulk-selection toolbar — no status filter (a human can
    re-check an already-verified record on purpose). Fire-and-forget like
    every other trigger endpoint; find the run_id via GET /ingestion/status
    (current_run.kind == "recheck_selected") once it starts, same as the
    live-progress panel already does for every other run kind.
    """
    import asyncio
    from processing.scout_controller import scout_controller

    if not request.ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    asyncio.create_task(scout_controller.run_recheck_selected(request.ids))
    return {"status": "started", "message": "Selected-startup recheck queued via controller"}


@router.post("/web-verify-selected")
async def trigger_web_verify_selected(request: SelectedIdsRequest):
    """
    Phase Q2 (29 Jul): web-verify an explicit, human-selected set of
    startups — not restricted to the no_source_excerpt backlog. Every
    review this produces is tagged with the run's run_id
    (GET /reviews?run_id=...) so results stay separable from the general
    Review Inbox, even though the response here doesn't carry it directly —
    same fire-and-forget contract as every other trigger endpoint.
    """
    import asyncio
    from processing.scout_controller import scout_controller

    if not request.ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    asyncio.create_task(scout_controller.run_web_verify_selected(request.ids))
    return {"status": "started", "message": "Selected-startup web-verify queued via controller"}


@router.get("/status")
async def verification_status(db: Session = Depends(get_db)):
    """
    Counts by verification_status, overall and by coarse source type, plus
    a `no_source_excerpt` count — the Phase W backlog (flagged records with
    no source_excerpt, i.e. nothing for H-3 to check them against) — broken
    out separately from other flagged reasons so its progress is a visible,
    concrete countdown as web-verify batches run.
    """
    rows = (
        db.query(Startup.verification_status, Startup.source, func.count(Startup.id))
        .group_by(Startup.verification_status, Startup.source)
        .all()
    )

    overall = {s: 0 for s in _STATUSES}
    by_source: dict = {}
    for status, source, count in rows:
        status = status if status in _STATUSES else "unverified"
        overall[status] += count
        bucket = _classify_source(source)
        by_source.setdefault(bucket, {s: 0 for s in _STATUSES})
        by_source[bucket][status] += count

    # The TRUE remaining web-verify queue must match exactly what
    # web_verify_pending() actually processes — flagged + no excerpt AND
    # reason still 'no_source_excerpt'. The looser "flagged + no excerpt"
    # count (used until 29 Jul) also swept in records web-verify had ALREADY
    # handled — they stay flagged with a staged correction and, by design,
    # never gain a stored excerpt (they're checked against live search, not
    # text) — so the dashboard countdown barely moved after a full drain and
    # read as a failure. Filtering on the reason makes it the honest count of
    # what's genuinely left to process.
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    no_source_excerpt = (
        db.query(func.count(Startup.id))
        .filter(Startup.verification_status == "flagged")
        .filter(Startup.source_excerpt.is_(None) | (Startup.source_excerpt == ""))
        .filter(cast(Startup.verification_evidence, JSONB)["reason"].astext == "no_source_excerpt")
        .scalar()
    ) or 0

    return {"overall": overall, "by_source": by_source, "no_source_excerpt": no_source_excerpt}
