"""
POST /dedup/run — the Browse "Deduplicate" button.

Dry-run by default (the project's convention for anything that deletes or
queues): the UI calls it with dry_run=true, shows the counts and the pairs,
and only then calls again with dry_run=false. See processing/dedup_run.py for
what a run does and refuses to do.
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


class DedupRunRequest(BaseModel):
    ids: Optional[List[str]] = Field(
        None, description="Startup ids to find duplicates OF. Omit or null for every record.")
    dry_run: bool = True
    include_review: bool = False
    limit: int = Field(100, ge=1, le=500, description="Max merges per call")


@router.post("/run")
async def run_dedup(request: DedupRunRequest):
    from processing.dedup_run import run

    if request.ids is not None and not request.ids:
        raise HTTPException(status_code=400, detail="ids is empty — select records, or omit ids to run on all")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: run(request.ids, apply=not request.dry_run,
                          include_review=request.include_review, limit=request.limit))
    if result.get("busy"):
        raise HTTPException(status_code=409, detail=result["error"])
    if "error" in result and "scope" not in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result
