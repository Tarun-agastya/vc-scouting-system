import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.thesis_loader import get_theses, get_taxonomy, add_thesis, delete_thesis

router = APIRouter()
logger = logging.getLogger(__name__)


class ThesisRequest(BaseModel):
    id: str
    name: str
    summary: str
    industries: List[str] = []
    tech_clusters: List[str] = []
    keywords: List[str] = []
    exclude_keywords: List[str] = []


@router.get("")
async def list_theses():
    """
    All active scouting theses (stakeholder + ad-hoc), from config/theses.yaml.
    Powers Browse's "Relevant to" dropdown (Phase V-3).
    """
    theses = get_theses(active_only=True)
    return {
        "theses": [
            {"id": t["id"], "name": t["name"], "kind": t["kind"], "summary": t["summary"]}
            for t in theses
        ]
    }


@router.get("/taxonomy")
async def taxonomy_for_form():
    """
    The controlled industries + grouped tech_clusters — powers the "Add
    theme" form's pick-lists so an ad-hoc theme can only ever be built from
    valid taxonomy values (Phase V-4).
    """
    tax = get_taxonomy()
    return {"industries": tax["industries"], "tech_clusters": tax["tech_clusters"]}


@router.post("")
async def add_thesis_route(request: ThesisRequest):
    """
    Add a new ad-hoc theme (Phase V-4) — e.g. "find construction startups".
    Instantly filters/ranks the existing DB and tags new arrivals; no code
    change, no restart. industries/tech_clusters must be from GET
    /theses/taxonomy.
    """
    try:
        thesis = add_thesis(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "thesis": thesis}


@router.delete("/{thesis_id}")
async def delete_thesis_route(thesis_id: str):
    """Remove an ad-hoc theme. Stakeholder theses are protected — 400 if attempted."""
    try:
        removed = delete_thesis(thesis_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail=f"thesis id '{thesis_id}' not found")
    return {"status": "ok", "deleted": thesis_id}
