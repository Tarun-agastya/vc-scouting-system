import logging
from typing import Optional, List
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
    """
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from reasoning.qwen_client import qwen_client

    try:
        query_vector = embedder.embed(request.query)

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
            ai_analysis = qwen_client.synthesize_scout_results(request.query, startups)
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
    """Retrieve a single startup by ID."""
    startup = db.query(Startup).filter(Startup.id == startup_id).first()
    if not startup:
        raise HTTPException(status_code=404, detail="Startup not found")
    return {
        "id": str(startup.id),
        "name": startup.name,
        "description": startup.description,
        "industry": startup.industry,
        "country": startup.country,
        "city": startup.city,
        "funding_stage": startup.funding_stage,
        "website": startup.website,
        "ai_summary": startup.ai_summary,
        "source": startup.source,
        "created_at": startup.created_at,
    }


@router.get("/list")
async def list_startups(
    limit: int = 50,
    offset: int = 0,
    industry: Optional[str] = None,
    country: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List startups from PostgreSQL with optional filters."""
    query = db.query(Startup)
    if industry:
        query = query.filter(Startup.industry.ilike(f"%{industry}%"))
    if country:
        query = query.filter(Startup.country.ilike(f"%{country}%"))
    total = query.count()
    startups = query.order_by(Startup.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "startups": [
            {
                "id": str(s.id),
                "name": s.name,
                "industry": s.industry,
                "country": s.country,
                "city": s.city,
                "funding_stage": s.funding_stage,
                "source": s.source,
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
