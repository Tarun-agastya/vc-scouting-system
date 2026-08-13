import uuid
import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request / Response Models ─────────────────────────────────────────────────

class InvestorProfileRequest(BaseModel):
    name: str
    type: str = "VC"
    focus_industries: List[str] = []
    focus_stages: List[str] = []
    focus_regions: List[str] = []
    thesis: Optional[str] = None
    limit: int = 10


class StartupMatchRequest(BaseModel):
    name: str
    description: str
    industry: Optional[str] = None
    country: Optional[str] = None
    funding_stage: Optional[str] = None
    limit: int = 5


class SaveInvestorRequest(BaseModel):
    name: str
    type: str = "VC"
    focus_industries: List[str] = []
    focus_stages: List[str] = []
    focus_regions: List[str] = []
    thesis: Optional[str] = None
    website: Optional[str] = None
    description: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/find-startups")
async def find_matching_startups(request: InvestorProfileRequest):
    """
    Core matchmaking: given an investor profile, find the best-fitting startups.
    Returns ranked matches with AI-generated rationale for the top 5.

    match_investor_to_startups() makes blocking Ollama calls (an embedding
    call plus up to 5 sequential rationale-generation calls) — dispatched
    via run_in_executor so they never freeze FastAPI's event loop for every
    other request, and under scout_controller's gpu_mutex so they never race
    a concurrent ingestion run for the same local Ollama backend (the same
    freeze/race /scout/search's docstring already describes fixing there —
    this route made the identical blocking, unguarded call directly).
    """
    import asyncio
    from matchmaking.engine import matchmaking_engine
    from processing.scout_controller import scout_controller

    loop = asyncio.get_event_loop()

    try:
        async with scout_controller.gpu_mutex:
            matches = await loop.run_in_executor(
                None, matchmaking_engine.match_investor_to_startups,
                request.model_dump(), request.limit,
            )
        return {
            "investor": request.name,
            "matches_found": len(matches),
            "matches": matches,
        }
    except Exception as exc:
        logger.error(f"[Matchmaking] find-startups failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/find-investors")
async def find_matching_investors(request: StartupMatchRequest):
    """
    Reverse matchmaking: given a startup, find the best-fitting investors.
    Requires investors to be saved via /matchmaking/save-investor first.

    match_startup_to_investors() makes a blocking Ollama embedding call (no
    rationale generation, so no gpu_mutex needed — same convention as
    /scout/search's plain embedding step) — dispatched via run_in_executor
    so it never freezes the event loop for every other request.
    """
    import asyncio
    from matchmaking.engine import matchmaking_engine

    loop = asyncio.get_event_loop()

    try:
        matches = await loop.run_in_executor(
            None, matchmaking_engine.match_startup_to_investors,
            request.model_dump(), request.limit,
        )
        return {
            "startup": request.name,
            "matches_found": len(matches),
            "matches": matches,
        }
    except Exception as exc:
        logger.error(f"[Matchmaking] find-investors failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/save-investor")
async def save_investor(request: SaveInvestorRequest):
    """
    Save an investor profile to PostgreSQL + Qdrant for reverse matching.
    """
    import asyncio
    from database.connection import SessionLocal
    from database.models import Investor
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store

    investor_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()

    try:
        # Embed investor profile — blocking Ollama call, dispatched via
        # run_in_executor so it never freezes the event loop (same
        # convention as /scout/search's embedding step).
        investor_text = embedder.build_investor_text(request.model_dump())
        vector = await loop.run_in_executor(None, embedder.embed, investor_text)

        # Save to PostgreSQL
        db = SessionLocal()
        db_investor = Investor(
            id=investor_id,
            name=request.name,
            type=request.type,
            focus_industries=request.focus_industries,
            focus_stages=request.focus_stages,
            focus_regions=request.focus_regions,
            thesis=request.thesis,
            website=request.website,
            description=request.description,
            embedding_id=investor_id,
        )
        db.add(db_investor)
        db.commit()
        db.close()

        # Save to Qdrant
        qdrant_store.upsert_investor(
            investor_id=investor_id,
            vector=vector,
            payload={
                "id": investor_id,
                "name": request.name,
                "type": request.type,
                "focus_industries": request.focus_industries,
                "focus_stages": request.focus_stages,
                "focus_regions": request.focus_regions,
                "thesis": request.thesis,
            },
        )

        return {"status": "saved", "id": investor_id}

    except Exception as exc:
        logger.error(f"[Matchmaking] save-investor failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
