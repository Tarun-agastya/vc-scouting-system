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
    """Scrape all configured accelerator portfolio pages."""
    from ingestion.sources import ACCELERATOR_SOURCES
    from ingestion.web_scraper import web_scraper

    for source in ACCELERATOR_SOURCES:
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source["url"],
            source_type="accelerator",
        )

    return {
        "status": "started",
        "message": f"Scraping {len(ACCELERATOR_SOURCES)} accelerator pages",
    }


@router.post("/scrape-universities")
async def scrape_universities(background_tasks: BackgroundTasks):
    """Scrape all configured university spinoff pages."""
    from ingestion.sources import UNIVERSITY_SOURCES
    from ingestion.web_scraper import web_scraper

    for source in UNIVERSITY_SOURCES:
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source["url"],
            source_type="university",
        )

    return {
        "status": "started",
        "message": f"Scraping {len(UNIVERSITY_SOURCES)} university pages",
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
    Run all ingestion pipelines: RSS + all accelerators + all universities.
    This is the 'big sweep' — use when you want to refresh everything.
    """
    from ingestion.rss_parser import rss_parser
    from ingestion.web_scraper import web_scraper
    from ingestion.sources import ACCELERATOR_SOURCES, UNIVERSITY_SOURCES

    # RSS feeds
    background_tasks.add_task(rss_parser.ingest_feeds, max_entries=50)

    # Accelerators
    for source in ACCELERATOR_SOURCES:
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source["url"],
            source_type="accelerator",
        )

    # Universities
    for source in UNIVERSITY_SOURCES:
        background_tasks.add_task(
            web_scraper.scrape_source,
            url=source["url"],
            source_type="university",
        )

    total_tasks = 1 + len(ACCELERATOR_SOURCES) + len(UNIVERSITY_SOURCES)
    return {
        "status": "started",
        "message": f"Full ingestion pipeline started — {total_tasks} tasks queued",
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
