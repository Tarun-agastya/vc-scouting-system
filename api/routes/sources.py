"""
Live source registry API — Phase S (dynamic sources).

Lets non-technical staff (and, later, the OpenClaw agent under human
confirmation) add or remove intelligence sources without touching a file
or writing code. Every write goes through config/source_loader.py, which
validates the entry and appends it to config/sources.yaml using a
comment-preserving round-trip — the next ingestion run picks it up with no
restart needed.
"""
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.source_loader import (
    SourceType,
    CrawlStrategy,
    Priority,
    add_rss_feed,
    add_web_source,
    delete_source,
    get_rss_feeds,
    get_web_sources,
    get_newsletter_senders,
    get_newsletter_search_terms,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request Models ────────────────────────────────────────────────────────────

class RSSFeedRequest(BaseModel):
    name: str
    url: str
    region: str = "europe"
    type: str = "news"


class WebSourceRequest(BaseModel):
    source_id:       str
    source_name:     str
    source_type:     SourceType
    location:        str = "Global"
    primary_url:     str
    crawl_strategy:  CrawlStrategy = CrawlStrategy.WEBSITE_SCRAPE
    crawl_frequency: str = "weekly"
    priority:        Priority = Priority.MEDIUM


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_sources():
    """
    Everything currently in config/sources.yaml — read fresh on every call.
    Powers the dashboard's source-management page.
    """
    return {
        "rss_feeds": get_rss_feeds(),
        "web_sources": [s.model_dump(mode="json") for s in get_web_sources()],
        "newsletter_senders": get_newsletter_senders(),
        "newsletter_search_terms": get_newsletter_search_terms(),
    }


@router.post("/rss")
async def add_rss_feed_route(request: RSSFeedRequest):
    """Add a new RSS feed to config/sources.yaml."""
    try:
        entry = add_rss_feed(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "feed": entry.model_dump()}


@router.post("/web")
async def add_web_source_route(request: WebSourceRequest):
    """
    Add a new web source (accelerator / incubator / university hub / etc.)
    to config/sources.yaml. source_id must be unique.
    """
    try:
        entry = add_web_source(request.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "source": entry.model_dump(mode="json")}


@router.delete("/web/{source_id}")
async def delete_web_source_route(source_id: str):
    """Remove a web source from config/sources.yaml by its source_id."""
    removed = delete_source(source_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"source_id '{source_id}' not found")
    return {"status": "ok", "deleted": source_id}
