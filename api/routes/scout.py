import logging
from typing import Optional, List
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, HttpUrl
from database.connection import get_db
from database.models import Startup, ScoutingSession
from processing.deduplicator import name_to_stable_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request / Response Models ─────────────────────────────────────────────────

class ScoutRequest(BaseModel):
    query: str
    country: Optional[str] = None
    industry: Optional[str] = None
    funding_stage: Optional[str] = None
    limit: int = 15


class StartupAddRequest(BaseModel):
    name: str
    description: str
    industry: Optional[str] = None
    sub_industry: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    funding_stage: Optional[str] = None
    website: Optional[str] = None
    source: Optional[str] = "manual"
    tags: Optional[List[str]] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/search")
async def search_startups(request: ScoutRequest, db: Session = Depends(get_db)):
    """
    Semantic search over the startup database.
    Returns AI-synthesized investor report + raw startup list.

    Both the embedding call and the reasoning-model synthesis are synchronous
    (blocking) Ollama calls — dispatched via run_in_executor so they never
    block FastAPI's event loop (previously a single search request would
    freeze the ENTIRE API — every other request, including simple keyword
    search and ingestion-status polling — for up to 120s). The synthesis
    call additionally acquires the GPU mutex so it never fights a concurrent
    ingestion run for the same Ollama backend — it politely waits its turn
    instead of racing and timing out.
    """
    import asyncio
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from reasoning.qwen_client import qwen_client
    from processing.scout_controller import scout_controller

    loop = asyncio.get_event_loop()

    try:
        query_vector = await loop.run_in_executor(None, embedder.embed, request.query)

        filters: dict = {}
        if request.country:
            filters["country"] = request.country
        if request.industry:
            filters["industry"] = request.industry
        if request.funding_stage:
            filters["funding_stage"] = request.funding_stage

        results = qdrant_store.search_startups(
            query_vector=query_vector,
            limit=request.limit,
            filters=filters if filters else None,
        )

        startups = [r.payload for r in results]

        if startups:
            async with scout_controller.gpu_mutex:
                ai_analysis = await loop.run_in_executor(
                    None, qwen_client.synthesize_scout_results, request.query, startups
                )
        else:
            ai_analysis = (
                "No matching startups found in the database. "
                "Try running /ingestion/rss or /ingestion/run-all to populate the database first."
            )

        # Log session
        session = ScoutingSession(
            query=request.query,
            filters=filters or {},
            results=startups[:5],  # Save only top-5 to avoid bloating DB
            result_count=len(startups),
            source="api",
        )
        db.add(session)
        db.commit()

        return {
            "query": request.query,
            "total_found": len(startups),
            "startups": startups,
            "ai_analysis": ai_analysis,
        }

    except Exception as exc:
        logger.error(f"[Scout] Search failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/add-startup")
async def add_startup(
    request: StartupAddRequest,
    background_tasks: BackgroundTasks,
):
    """
    Manually add a startup to PostgreSQL and Qdrant.

    Uses the same deduplication pipeline as automated ingestion:
      fingerprint = sha256(normalized_name | domain)
      stable UUID  = uuid5(NAMESPACE_URL, fingerprint)

    If the startup already exists, its record is enriched and
    source_history is extended rather than creating a duplicate row.
    AI analysis runs in the background after the response is returned.
    """
    from processing.storage import upsert_startup

    # Derive the stable ID up-front so we can return it in error responses too
    stable_id = name_to_stable_uuid(request.name, request.website or "")
    if not stable_id:
        raise HTTPException(status_code=422, detail="Startup name is required")

    try:
        record_id, _ = upsert_startup(
            startup=request.model_dump(),
            source=request.source or "manual",
            source_url=request.website or "",
        )
    except Exception as exc:
        logger.error(f"[Scout] /add-startup failed for '{request.name}': {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    if record_id is None:
        # upsert_startup returns None when the embedder (Ollama) is unreachable
        raise HTTPException(
            status_code=503,
            detail="Could not store startup — is Ollama running?",
        )

    # AI analysis in background (does not block the response)
    background_tasks.add_task(_run_ai_analysis, record_id)

    return {
        "status": "ok",
        "id": record_id,
        "message": "Startup saved. AI analysis running in background.",
    }


@router.get("/startup/{startup_id}")
async def get_startup(startup_id: str, db: Session = Depends(get_db)):
    """Retrieve a single startup by ID (full record, for the detail/edit view)."""
    s = db.query(Startup).filter(Startup.id == startup_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Startup not found")
    return {
        "id": str(s.id),
        "name": s.name,
        "short_description": s.short_description,
        "description": s.description,
        "website": s.website,
        "industry": s.industry,
        "sub_industry": s.sub_industry,
        "tech_cluster": s.tech_cluster,
        "country": s.country,
        "city": s.city,
        "address": s.address,
        "funding_stage": s.funding_stage,
        "founded_year": s.founded_year,
        "employee_count": s.employee_count,
        "contact_info": s.contact_info,
        "linkedin": s.linkedin,
        "tags": s.tags or [],
        "ai_summary": s.ai_summary,
        "enrichment_score": s.enrichment_score,
        "score_tier": s.score_tier,
        "source_confidence": s.source_confidence,
        "score_breakdown": s.score_breakdown,
        "source": s.source,
        "source_url": s.source_url,
        "source_history": s.source_history or [],
        "extracted_at": s.extracted_at,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "verification_status": s.verification_status or "unverified",
        "verification_notes": s.verification_notes,
        "verification_evidence": s.verification_evidence,
        "verified_at": s.verified_at,
        "source_excerpt": s.source_excerpt,
    }


@router.patch("/startup/{startup_id}")
async def edit_startup(startup_id: str, changes: dict, db: Session = Depends(get_db)):
    """
    Apply a manual staff edit directly to a startup (data-stewardship: a human
    editing IS the reviewer, so this applies immediately — unlike the pipeline,
    which stages). Records the edit in source_history and re-indexes Qdrant.
    Only whitelisted fields may be changed.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from datetime import datetime

    s = db.query(Startup).filter(Startup.id == startup_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Startup not found")

    editable = {
        "name", "short_description", "description", "website", "industry",
        "sub_industry", "tech_cluster", "country", "city", "address",
        "funding_stage", "founded_year", "employee_count", "contact_info",
        "linkedin", "tags",
    }
    applied = {}
    for field, value in (changes or {}).items():
        if field not in editable:
            continue
        setattr(s, field, value)
        applied[field] = value

    if not applied:
        raise HTTPException(status_code=422, detail="No editable fields supplied")

    # Audit the manual edit in source_history
    history = list(s.source_history or [])
    history.append({
        "source": "manual-edit",
        "source_name": "Team edit (dashboard)",
        "fields": list(applied.keys()),
        "extracted_at": datetime.utcnow().isoformat(),
    })
    s.source_history = history
    flag_modified(s, "source_history")
    s.updated_at = datetime.utcnow()
    db.commit()

    # Re-index in Qdrant so search reflects the edit
    try:
        from api.routes.reviews import _reindex
        _reindex(db, s)
    except Exception as exc:
        logger.warning(f"[Scout] Re-index after edit failed for {startup_id}: {exc}")

    return {"status": "ok", "id": startup_id, "applied": applied}


@router.delete("/startup/{startup_id}")
async def delete_startup(startup_id: str, confirm: bool = False, db: Session = Depends(get_db)):
    """
    Permanently delete a startup from PostgreSQL and Qdrant (manual, staff-driven).

    Safety: without ?confirm=true this returns the record for review instead of
    deleting. With confirm=true it removes the row + its Qdrant point, cleans up
    any related review/suppression rows, and logs the deletion.
    """
    from database.models import DuplicateReview, SuppressedMatch
    from vector_db.qdrant_store import qdrant_store

    s = db.query(Startup).filter(Startup.id == startup_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Startup not found")

    if not confirm:
        return {
            "status": "confirm_required",
            "message": "Pass ?confirm=true to permanently delete this startup.",
            "startup": {"id": str(s.id), "name": s.name, "website": s.website,
                        "industry": s.industry, "country": s.country},
        }

    name = s.name
    # Clean up orphaned reviews / suppressions referencing this record
    db.query(DuplicateReview).filter(
        (DuplicateReview.master_id == startup_id) | (DuplicateReview.incoming_id == startup_id)
    ).delete(synchronize_session=False)
    db.query(SuppressedMatch).filter(
        (SuppressedMatch.master_id == startup_id) | (SuppressedMatch.other_id == startup_id)
    ).delete(synchronize_session=False)

    db.delete(s)
    db.commit()

    try:
        qdrant_store.delete_startup(startup_id)
    except Exception as exc:
        logger.warning(f"[Scout] Qdrant delete failed for {startup_id}: {exc}")

    logger.info(f"[Scout] DELETED startup '{name}' ({startup_id}) — manual/staff action")
    return {"status": "deleted", "id": startup_id, "name": name}


def _domain_label(source: Optional[str], source_url: Optional[str]) -> str:
    """
    Human-readable "which website" label for one startup: the registrable
    domain of source_url (e.g. "munich-startup.de") when there is a URL to
    parse, else the coarse source bucket (e.g. "newsletter", "manual") as a
    fallback for sources that were never a specific web page.
    """
    if source_url and not source_url.startswith("gmail://"):
        host = urlparse(source_url).netloc
        if host:
            return host[4:] if host.startswith("www.") else host
        return source_url
    return source or "unknown"


@router.get("/source-sites")
async def list_source_sites(db: Session = Depends(get_db)):
    """
    Distinct source websites actually present in the database, with counts —
    NOT the configured registry (see GET /sources for that; a site can be
    configured but never have produced a stored record, or vice versa after
    a config edit). Powers Browse's "source website" filter dropdown, so a
    reviewer can work through one site's extractions at a time for manual
    verification instead of hunting for the right free-text filter value.
    """
    from collections import Counter

    rows = db.query(Startup.source, Startup.source_url).all()
    counts = Counter(_domain_label(source, source_url) for source, source_url in rows)

    return {
        "sites": [
            {"label": label, "count": count}
            for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    }


@router.get("/list")
async def list_startups(
    q: Optional[str] = None,               # keyword: name / summary / description / tags
    industry: Optional[str] = None,
    country: Optional[str] = None,
    city: Optional[str] = None,
    tech_cluster: Optional[str] = None,
    employee_count: Optional[str] = None,
    funding_stage: Optional[str] = None,
    score_tier: Optional[str] = None,
    source: Optional[str] = None,
    source_url: Optional[str] = None,      # filter by the specific site a record was extracted from
    verification_status: Optional[str] = None,   # unverified | verified | flagged (Phase H-2)
    founded_year_min: Optional[int] = None,
    founded_year_max: Optional[int] = None,
    sort: str = "created_at",              # created_at | extracted_at | name | score
    order: str = "desc",                   # asc | desc
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """
    Browse/search startups with rich filters + keyword search (Phase D).
    Powers the team dashboard's Browse & Search page. Keyword `q` matches
    name / short_description / description / tags (case-insensitive).
    """
    from sqlalchemy import or_, cast, String

    query = db.query(Startup)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Startup.name.ilike(like),
            Startup.short_description.ilike(like),
            Startup.description.ilike(like),
            cast(Startup.tags, String).ilike(like),
        ))
    if industry:       query = query.filter(Startup.industry.ilike(f"%{industry}%"))
    if country:        query = query.filter(Startup.country.ilike(f"%{country}%"))
    if city:           query = query.filter(Startup.city.ilike(f"%{city}%"))
    if tech_cluster:   query = query.filter(Startup.tech_cluster.ilike(f"%{tech_cluster}%"))
    if employee_count: query = query.filter(Startup.employee_count == employee_count)
    if funding_stage:  query = query.filter(Startup.funding_stage.ilike(f"%{funding_stage}%"))
    if score_tier:     query = query.filter(Startup.score_tier == score_tier)
    if source:         query = query.filter(Startup.source.ilike(f"%{source}%"))
    if source_url:
        # Matches both a real site domain ("munich-startup.de") AND a
        # fallback bucket label like "newsletter" (whose rows have no
        # crawlable source_url — see _domain_label) — the /source-sites
        # dropdown can hand back either kind of label.
        query = query.filter(or_(
            Startup.source_url.ilike(f"%{source_url}%"),
            Startup.source.ilike(f"%{source_url}%"),
        ))
    if verification_status: query = query.filter(Startup.verification_status == verification_status)
    if founded_year_min is not None: query = query.filter(Startup.founded_year >= founded_year_min)
    if founded_year_max is not None: query = query.filter(Startup.founded_year <= founded_year_max)

    total = query.count()

    sort_col = {
        "created_at": Startup.created_at,
        "extracted_at": Startup.extracted_at,
        "name": Startup.name,
        "score": Startup.enrichment_score,
    }.get(sort, Startup.created_at)
    sort_col = sort_col.asc() if order == "asc" else sort_col.desc()

    startups = query.order_by(sort_col).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "startups": [
            {
                "id": str(s.id),
                "name": s.name,
                "short_description": s.short_description,
                "industry": s.industry,
                "tech_cluster": s.tech_cluster,
                "country": s.country,
                "city": s.city,
                "funding_stage": s.funding_stage,
                "employee_count": s.employee_count,
                "score_tier": s.score_tier,
                "enrichment_score": s.enrichment_score,
                "source": s.source,
                "source_url": s.source_url,
                "extracted_at": s.extracted_at,
                "created_at": s.created_at,
                "verification_status": s.verification_status or "unverified",
            }
            for s in startups
        ],
    }


@router.post("/sector-report")
async def sector_report(sector: str, db: Session = Depends(get_db)):
    """Generate a sector intelligence report from stored startups."""
    from reasoning.analyzer import generate_sector_report

    startups = (
        db.query(Startup)
        .filter(Startup.industry.ilike(f"%{sector}%"))
        .limit(30)
        .all()
    )

    if not startups:
        raise HTTPException(
            status_code=404,
            detail=f"No startups found for sector '{sector}'. Run ingestion first.",
        )

    startup_dicts = [
        {
            "name": s.name,
            "description": s.description,
            "country": s.country,
            "industry": s.industry,
            "funding_stage": s.funding_stage,
        }
        for s in startups
    ]

    report = generate_sector_report(sector, startup_dicts)
    return {"sector": sector, "startups_analyzed": len(startups), "report": report}


# ── Background Task ───────────────────────────────────────────────────────────

def _run_ai_analysis(startup_id: str):
    """Background task: enrich a startup with AI analysis."""
    from reasoning.analyzer import enrich_startup_in_db
    enrich_startup_in_db(startup_id)
