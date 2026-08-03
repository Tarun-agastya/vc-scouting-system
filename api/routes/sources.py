"""
Live source registry API — Phase S (dynamic sources).

Lets non-technical staff (and, later, the OpenClaw agent under human
confirmation) add or remove intelligence sources without touching a file
or writing code. Every write goes through config/source_loader.py, which
validates the entry and appends it to config/sources.yaml using a
comment-preserving round-trip — the next ingestion run picks it up with no
restart needed.

Phase R-2 (31 Jul) adds the SiteProfile endpoints below: what the adaptive
pipeline's structural inspector has LEARNED about each source, kept separate
from the human-authored registry above (probing must never silently overwrite
a hand edit). Nothing in the live crawl path reads these yet — that's R-4 —
so today they only power the dashboard's Shape/Recall columns and the manual
Re-inspect / pin / batch-profile actions.
"""
import asyncio
import logging
from datetime import datetime
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
from processing.site_profile_store import (
    list_profiles,
    probe_and_store,
    set_pinned,
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


class ReinspectRequest(BaseModel):
    source_id: Optional[str] = None
    url: Optional[str] = None


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


# ── SiteProfile routes (Phase R-2) ──────────────────────────────────────────

def _profile_to_dict(row) -> dict:
    return {
        "id": str(row.id),
        "domain": row.domain,
        "url_pattern": row.url_pattern,
        "source_id": row.source_id,
        "page_shape": row.page_shape,
        "text_extraction": row.text_extraction,
        "chunking_mode": row.chunking_mode,
        "needs_render": row.needs_render,
        "paginate": row.paginate,
        "follow_detail_links": row.follow_detail_links,
        "detail_link_pattern": row.detail_link_pattern,
        "bypass_candidate_filter": row.bypass_candidate_filter,
        "expected_entity_count": row.expected_entity_count,
        "strategy_source": row.strategy_source,
        "confidence": row.confidence,
        "reason": row.reason,
        "probe_version": row.probe_version,
        "probed_at": row.probed_at.isoformat() if row.probed_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "status": row.status,
        "flag_reason": row.flag_reason,
        "last_expected": row.last_expected,
        "last_extracted": row.last_extracted,
        "recall_ratio": row.recall_ratio,
        "consecutive_shortfalls": row.consecutive_shortfalls,
    }


@router.get("/profiles")
async def list_site_profiles():
    """Every learned SiteProfile — powers the dashboard's Shape/Recall columns."""
    return {"profiles": [_profile_to_dict(r) for r in list_profiles()]}


@router.post("/profiles/reinspect")
async def reinspect_source(request: ReinspectRequest):
    """
    Force a fresh probe of one source (by registry source_id, or an ad-hoc
    URL) right now, bypassing the TTL/version cache. A pinned profile is
    left untouched — unpin it first to let a probe change its strategy.
    """
    url = request.url
    if not url and request.source_id:
        match = [s for s in get_web_sources() if s.source_id == request.source_id]
        if not match:
            raise HTTPException(status_code=404, detail=f"source_id '{request.source_id}' not found")
        url = match[0].primary_url
    if not url:
        raise HTTPException(status_code=422, detail="source_id or url is required")

    try:
        row = await probe_and_store(url, force=True)
    except Exception as exc:
        logger.error(f"[SiteProfile] re-inspect failed for {url}: {exc}")
        raise HTTPException(status_code=502, detail=f"probe failed: {exc}")
    return {"status": "ok", "profile": _profile_to_dict(row)}


@router.post("/profiles/{profile_id}/pin")
async def pin_site_profile(profile_id: str):
    """Freeze a profile's strategy — never auto-re-probed or auto-overwritten.
    The dashboard equivalent of the old sources.yaml render_mode: 'always'."""
    row = set_pinned(profile_id, True)
    if row is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"status": "ok", "profile": _profile_to_dict(row)}


@router.post("/profiles/{profile_id}/unpin")
async def unpin_site_profile(profile_id: str):
    row = set_pinned(profile_id, False)
    if row is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"status": "ok", "profile": _profile_to_dict(row)}


# In-memory progress for the batch-profile action — lightweight on purpose.
# This doesn't touch the GPU mutex (R-2 is deterministic-only, no LLM), so it
# doesn't need scout_controller's RunRecord machinery; the frontend polls
# batch-status the same way it polls /ingestion/status for a real run.
_batch_state = {
    "running": False, "total": 0, "done": 0, "failed": 0,
    "current_url": None, "started_at": None, "finished_at": None,
}


async def _run_profile_batch(urls: list) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for url in urls:
            _batch_state["current_url"] = url
            try:
                await probe_and_store(url, client=client)
            except Exception as exc:
                _batch_state["failed"] += 1
                logger.warning(f"[SiteProfile] batch probe failed for {url}: {exc}")
            _batch_state["done"] += 1
    _batch_state["running"] = False
    _batch_state["current_url"] = None
    _batch_state["finished_at"] = datetime.utcnow().isoformat()


@router.post("/profiles/batch")
async def batch_profile_sources():
    """
    "Profile all sources" — probes every registered web source once, fire-
    and-forget. The cheapest possible generalization check: 22 real sources
    classified at zero extraction cost, no LLM, no GPU mutex.
    """
    if _batch_state["running"]:
        raise HTTPException(status_code=409, detail="A profiling batch is already running")
    urls = [s.primary_url for s in get_web_sources()]
    _batch_state.update(
        running=True, total=len(urls), done=0, failed=0,
        current_url=None, started_at=datetime.utcnow().isoformat(), finished_at=None,
    )
    asyncio.create_task(_run_profile_batch(urls))
    return {"status": "started", "total": len(urls)}


@router.get("/profiles/batch-status")
async def batch_profile_status():
    return dict(_batch_state)
