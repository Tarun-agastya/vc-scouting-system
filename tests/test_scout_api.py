"""Scout API: search/filter/list, PATCH edit, DELETE (Phase C + D)."""
import asyncio
from database.connection import SessionLocal
from database.models import Startup
from api.routes import scout as S


def _run(coro):
    return asyncio.run(coro)


def test_keyword_search_and_filter(make, db):
    make("Search Widget", website="pytest-search-1.com", city="Munich",
         country="Germany", industry="Robotics", description="warehouse picking robots")
    res = _run(S.list_startups(q="PYTEST Search Widget", db=SessionLocal()))
    names = [s["name"] for s in res["startups"]]
    assert any("PYTEST Search Widget" in n for n in names)
    # country filter narrows results
    res2 = _run(S.list_startups(q="PYTEST Search Widget", country="Germany", db=SessionLocal()))
    assert res2["total"] >= 1


def test_edit_applies_whitelisted_only(make, db):
    rid, _ = make("Edit Me", website="pytest-edit.com", city="Bonn", description="widgets")
    res = _run(S.edit_startup(rid, {"funding_stage": "Series A", "city": "Cologne",
                                    "bogus_field": "x"}, db=SessionLocal()))
    assert set(res["applied"]) == {"funding_stage", "city"}  # bogus dropped
    db.expire_all()
    row = db.query(Startup).filter(Startup.id == rid).first()
    assert row.funding_stage == "Series A" and row.city == "Cologne"


def test_edit_requires_a_field(make):
    rid, _ = make("Edit Empty", website="pytest-edit-empty.com")
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _run(S.edit_startup(rid, {"nonexistent": "x"}, db=SessionLocal()))


def test_delete_confirm_flow(make, db):
    rid, _ = make("Delete Me", website="pytest-delete.com", city="Bonn")
    # without confirm -> not deleted
    r1 = _run(S.delete_startup(rid, confirm=False, db=SessionLocal()))
    assert r1["status"] == "confirm_required"
    assert db.query(Startup).filter(Startup.id == rid).first() is not None
    # with confirm -> gone from PG (and Qdrant)
    r2 = _run(S.delete_startup(rid, confirm=True, db=SessionLocal()))
    assert r2["status"] == "deleted"
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first() is None
    from vector_db.qdrant_store import qdrant_store
    assert len(qdrant_store._get_client().retrieve(collection_name="startups", ids=[rid])) == 0
