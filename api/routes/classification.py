"""
Controlled-taxonomy classification API (Phase V-2, 27 Jul).

  POST /classification/reclassify   trigger a reclassification batch through
                                     the controller (queues on the GPU mutex,
                                     returns immediately)
  GET  /classification/status       how many startups are classified into
                                     config/taxonomy.yaml's controlled
                                     vocabulary vs still pending
"""
import logging
from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Startup

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/reclassify")
async def trigger_reclassify(limit: int = 20):
    """Queue a reclassification batch through the controller (GPU mutex)."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_reclassify(limit=limit))
    return {"status": "started", "message": "Reclassification batch queued via controller"}


@router.get("/status")
async def classification_status(db: Session = Depends(get_db)):
    """
    Counts: how much of the DB is on the controlled taxonomy vs still
    pending. Must match reclassify_pending()'s own definition of "pending"
    exactly, or this becomes a repeat of the same misleading-counter bug
    already found and fixed once this session on the web-verify backlog:
    classified_at IS NULL is the primary marker, but Phase Q1 (29 Jul)
    extended what one pass produces (added business_model) without
    resetting classified_at on the ~690 records classified before that —
    they'd otherwise read "0 pending" forever despite genuinely lacking it.
    """
    total = db.query(Startup).count()
    pending = db.query(Startup).filter(
        or_(Startup.classified_at.is_(None), Startup.business_model.is_(None))
    ).count()
    return {"total": total, "classified": total - pending, "pending": pending}
