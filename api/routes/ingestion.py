import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request Models ────────────────────────────────────────────────────────────

class RSSIngestionRequest(BaseModel):
    feed_urls: Optional[List[str]] = None  # reserved; controller runs all default feeds
    max_entries: int = 50


class ScrapeRequest(BaseModel):
    url: str
    source_type: str = "accelerator"  # accelerator | incubator | university | general


class TargetedRequest(BaseModel):
    """
    The agent's lever (Phase 4). Provide exactly one target:
      - kind="rss" or kind="newsletter" for those whole-source sweeps, OR
      - source_id matching a config/source_registry entry, OR
      - url for an ad-hoc web scrape.
    """
    kind:        Optional[str] = None        # "rss" | "newsletter"
    source_id:   Optional[str] = None        # registry source_id
    url:         Optional[str] = None         # ad-hoc URL
    source_type: str = "general"


# ── Routes ────────────────────────────────────────────────────────────────────
# Every ingestion path goes through scout_controller so the GPU mutex serializes
# all heavy LLM work. Runs are launched as background tasks that queue on the
# mutex; the endpoint returns immediately.

@router.post("/rss")
async def ingest_rss_feeds(request: RSSIngestionRequest):
    """Trigger RSS ingestion through the controller (queues on the GPU mutex)."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_rss(max_entries=request.max_entries))
    return {"status": "started", "message": "RSS ingestion queued via controller"}


@router.post("/scrape")
async def scrape_website(request: ScrapeRequest):
    """Scrape a single source URL through the controller."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(
        scout_controller.run_web_source(request.url, request.source_type)
    )
    return {
        "status": "started",
        "message": f"Scraping {request.url} as '{request.source_type}' (queued via controller)",
    }


@router.post("/scrape-accelerators")
async def scrape_all_accelerators():
    """Scrape all HIGH-priority accelerator/incubator/hub pages, sequentially."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_accelerators())
    return {"status": "started", "message": "Accelerator/hub sweep queued via controller"}


@router.post("/scrape-universities")
async def scrape_universities():
    """Scrape all HIGH-priority university hub pages, sequentially."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_universities())
    return {"status": "started", "message": "University hub sweep queued via controller"}


@router.post("/newsletters")
async def ingest_newsletters():
    """Trigger Gmail newsletter ingestion through the controller."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_newsletters())
    return {"status": "started", "message": "Gmail newsletter ingestion queued via controller"}


@router.post("/run-all")
async def run_full_ingestion():
    """Run the big sweep: RSS → accelerators → universities → newsletters."""
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_all())
    return {"status": "started", "message": "Full ingestion sweep queued via controller"}


@router.post("/targeted")
async def ingest_targeted(request: TargetedRequest):
    """
    The agent's command lever: run ONE focused ingestion under the mutex and
    return a run_id. Poll GET /ingestion/status?run_id=<id> until it finishes.
    """
    from processing.scout_controller import scout_controller

    try:
        run_id = scout_controller.submit_targeted(
            kind=request.kind,
            source_id=request.source_id,
            url=request.url,
            source_type=request.source_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"status": "started", "run_id": run_id}


@router.get("/status")
async def ingestion_status(run_id: Optional[str] = None):
    """
    Controller state: current run, last finished run, recent history, and GPU
    mutex state. Pass ?run_id=<id> to fetch a single run (for polling targeted
    requests). Also includes the current vector-DB startup count.
    """
    from processing.scout_controller import scout_controller

    payload = scout_controller.status(run_id=run_id)

    if run_id is None:
        try:
            from vector_db.qdrant_store import qdrant_store
            payload["startups_in_vector_db"] = qdrant_store.get_startup_count()
        except Exception as exc:
            payload["startups_in_vector_db"] = None
            payload["vector_db_error"] = str(exc)

    return payload
