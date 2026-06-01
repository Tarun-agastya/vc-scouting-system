import logging
from typing import Optional, List
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request Models ────────────────────────────────────────────────────────────

class RSSIngestionRequest(BaseModel):
    feed_urls: Optional[List[str]] = None  # None = use all default feeds
    max_entries: int = 50


class ScrapeRequest(BaseModel):
    url: str
    source_type: str = "accelerator"  # accelerator | incubator | university | general


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/rss")
async def ingest_rss_feeds(request: RSSIngestionRequest, background_tasks: BackgroundTasks):
    """Trigger RSS ingestion from startup news feeds (runs in background)."""
    from ingestion.rss_parser import rss_parser

    background_tasks.add_task(
        rss_parser.ingest_feeds,
        feed_urls=request.feed_urls,
        max_entries=request.max_entries,
    )
    return {
        "status": "started",
        "message": f"RSS ingestion started — processing {'default feeds' if not request.feed_urls else len(request.feed_urls)} feeds",
    }


@router.post("/scrape")
async def scrape_website(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """Scrape a single startup source URL (runs in background)."""
    from ingestion.web_scraper import web_scraper

    # Use the async method directly — FastAPI awaits coroutine background tasks
    background_tasks.add_task(
        web_scraper.scrape_source,
        url=request.url,
        source_type=request.source_type,
    )
    return {
        "status": "started",
        "message": f"Scraping {request.url} as '{request.source_type}'",
    }


@router.post("/scrape-accelerators")
async def scrape_all_accelerators(background_tasks: BackgroundTasks):
    """Scrape all HIGH-priority accelerator/incubator/hub pages from the source registry."""
    from config.source_registry import get_high_priority_sources, SourceType
    from ingestion.web_scraper import web_scraper

    sources = [
        s for s in get_high_priority_sources()
        if s.source_type != SourceType.UNIVERSITY_HUB
    ]
    for source in sources:
        logger.info(f"[SOURCE] {source.source_name}")
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source.primary_url,
            source_type=source.source_type.value,
        )
    return {
        "status": "started",
        "message": f"Scraping {len(sources)} accelerator/hub pages from registry",
        "sources": [s.source_name for s in sources],
    }


@router.post("/scrape-universities")
async def scrape_universities(background_tasks: BackgroundTasks):
    """Scrape all HIGH-priority university hub pages from the source registry."""
    from config.source_registry import get_high_priority_sources, SourceType
    from ingestion.web_scraper import web_scraper

    sources = [
        s for s in get_high_priority_sources()
        if s.source_type == SourceType.UNIVERSITY_HUB
    ]
    for source in sources:
        logger.info(f"[SOURCE] {source.source_name}")
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source.primary_url,
            source_type=source.source_type.value,
        )
    return {
        "status": "started",
        "message": f"Scraping {len(sources)} university hub pages from registry",
        "sources": [s.source_name for s in sources],
    }


@router.post("/newsletters")
async def ingest_newsletters(background_tasks: BackgroundTasks):
    """
    Trigger Gmail newsletter ingestion.
    Requires gmail_credentials.json in ./credentials/ directory.
    """
    from ingestion.newsletter_ingestor import newsletter_ingestor

    background_tasks.add_task(newsletter_ingestor.run_ingestion)
    return {
        "status": "started",
        "message": "Gmail newsletter ingestion started",
    }


@router.post("/run-all")
async def run_full_ingestion(background_tasks: BackgroundTasks):
    """
    Run all ingestion pipelines: RSS feeds + all HIGH-priority registry sources.
    This is the 'big sweep' — use when you want to refresh everything.
    """
    from ingestion.rss_parser import rss_parser
    from ingestion.web_scraper import web_scraper
    from config.source_registry import get_high_priority_sources

    # RSS feeds
    background_tasks.add_task(rss_parser.ingest_feeds, max_entries=50)

    # All HIGH priority sources — accelerators, incubators, hubs, universities
    sources = get_high_priority_sources()
    for source in sources:
        logger.info(f"[SOURCE] {source.source_name}")
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source.primary_url,
            source_type=source.source_type.value,
        )

    total_tasks = 1 + len(sources)
    return {
        "status": "started",
        "message": f"Full ingestion pipeline started — {total_tasks} tasks queued",
        "registry_sources": [s.source_name for s in sources],
    }


@router.get("/status")
async def ingestion_status():
    """Check current database size."""
    from vector_db.qdrant_store import qdrant_store

    try:
        count = qdrant_store.get_startup_count()
        return {"startups_in_vector_db": count}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Qdrant unavailable: {exc}")
