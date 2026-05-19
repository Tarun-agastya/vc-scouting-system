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
    """
    from matchmaking.engine import matchmaking_engine

    try:
        matches = matchmaking_engine.match_investor_to_startups(
            investor_profile=request.model_dump(),
            limit=request.limit,
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
    """
    from matchmaking.engine import matchmaking_engine

    try:
        matches = matchmaking_engine.match_startup_to_investors(
            startup=request.model_dump(),
            limit=request.limit,
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
    from database.connection import SessionLocal
    from database.models import Investor
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store

    investor_id = str(uuid.uuid4())

    try:
        # Embed investor profile
        investor_text = embedder.build_investor_text(request.model_dump())
        vector = embedder.embed(investor_text)

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
