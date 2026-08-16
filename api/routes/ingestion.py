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
    """
    Trigger RSS ingestion through the controller (queues on the GPU mutex).

    Chained (Phase AR-1, 12 Aug 2026): whatever this run extracts is
    followed immediately by a local-only recheck (H-3, no search call, no
    external quota) — same-day verification instead of waiting for the
    03:00 nightly job.
    """
    import asyncio
    from processing.scout_controller import scout_controller

    asyncio.create_task(scout_controller.run_rss_then_recheck(max_entries=request.max_entries))
    return {"status": "started", "message": "RSS ingestion queued via controller"}


@router.post("/scrape")
async def scrape_website(request: ScrapeRequest):
    """Scrape a single source URL through the controller."""
    import asyncio
    from processing.scout_controller import scout_controller

    # Chained (Phase X-4): name-only stubs this scrape produces are web-verified
    # immediately after it finishes, rather than waiting for the nightly job.
    asyncio.create_task(
        scout_controller.run_web_source_then_verify(request.url, request.source_type)
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
async def ingest_newsletters(days: int = 90, max_messages: int = 50):
    """
    Trigger Gmail newsletter ingestion through the controller.

    days=90 (default, raised from 14 on 16 Aug 2026). The 14-day window was
    silently losing mail: measured live that day, the mailbox held 103
    messages spanning June-August but only 32 were reachable at days=14, and
    all 29 June newsletters had never been ingested at all. Newsletters are
    the richest source this pipeline has, so that was the worst place for a
    quiet drop.

    90 days is affordable because the already-seen check now happens before
    any body fetch or LLM call — the ingestor resolves Message-IDs with one
    header-only pass and skips known ones — so a wider window costs a cheap
    header fetch, not a re-ingestion.

    Pass a larger value (e.g. days=3650) to sweep the whole mailbox. Safe to
    re-run at any size; already-ingested messages are always skipped, now by
    stable Message-ID rather than a mailbox-scoped IMAP UID.
    """
    import asyncio
    from processing.scout_controller import scout_controller

    # Chained (Phase AR-1, 12 Aug 2026) — same reasoning as /ingestion/rss above.
    asyncio.create_task(scout_controller.run_newsletters_then_recheck(max_messages=max_messages, days=days))
    window = f"routine {days}-day" if days <= 90 else f"backfill, {days}-day"
    return {"status": "started", "message": f"Gmail newsletter ingestion queued ({window} window)"}


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


@router.post("/stop")
async def stop_ingestion(run_id: Optional[str] = None):
    """
    Cancel the currently-running ingestion (or a specific run_id). Best-effort
    — see ScoutController.stop_run's docstring for the honest limitation
    around an Ollama call already in flight. Returns 409 if there was
    nothing to stop (already finished, or nothing running).
    """
    from processing.scout_controller import scout_controller

    stopped = scout_controller.stop_run(run_id)
    if not stopped:
        raise HTTPException(status_code=409, detail="No run in progress to stop")
    return {"status": "stopping"}


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
