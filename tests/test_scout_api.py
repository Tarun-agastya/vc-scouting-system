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


def test_semantic_search_fast_path_never_calls_the_llm(make, monkeypatch):
    """
    16 Sep 2026: /scout/search returned the matches AND a 14B-written
    investor report in one response, so the result list waited on the report.
    The report acquires the same GPU mutex as ingestion, which meant a search
    during a sweep blocked for minutes and read as a broken search — the
    owner reported exactly that ("semantic searching most of the times
    failing with api timeouts").

    synthesize=False must return the matches without going anywhere near
    Ollama. Proven by making the synthesizer raise: if the fast path touches
    it at all, this test fails rather than quietly getting slower.
    """
    from reasoning.qwen_client import qwen_client

    make("Fastpath Co", website="pytest-fastpath.com", city="Munich",
         country="Germany", industry="GreenTech",
         description="hydrogen electrolyser stacks for industrial heat")

    def _explode(*a, **kw):
        raise AssertionError("synthesize_scout_results must NOT be called when "
                             "synthesize=False — that is the whole point of the fast path")

    monkeypatch.setattr(qwen_client, "synthesize_scout_results", _explode)

    req = S.ScoutRequest(query="hydrogen electrolyser", limit=5, synthesize=False)
    res = _run(S.search_startups(req, db=SessionLocal()))

    assert res["ai_analysis"] is None, "fast path must not return a report"
    assert "startups" in res and isinstance(res["startups"], list)
    assert res["total_found"] == len(res["startups"])


def test_semantic_search_defaults_to_synthesizing(make, monkeypatch):
    """
    The flag defaults to True so every existing API consumer — scripts, the
    Discord bot, anything calling /scout/search directly — keeps getting the
    report it already depends on. Only the dashboard opts out.
    """
    from reasoning.qwen_client import qwen_client

    make("Default Synth Co", website="pytest-defsynth.com", city="Ulm",
         country="Germany", industry="GreenTech",
         description="recycling process for lithium battery cells")

    calls = []
    monkeypatch.setattr(qwen_client, "synthesize_scout_results",
                        lambda query, startups: calls.append(query) or "REPORT")

    assert S.ScoutRequest(query="x").synthesize is True

    req = S.ScoutRequest(query="lithium battery recycling", limit=5)
    res = _run(S.search_startups(req, db=SessionLocal()))
    if res["total_found"]:
        assert calls, "default path must call the synthesizer"
        assert res["ai_analysis"] == "REPORT"
