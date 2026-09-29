"""
The research loop (processing/review_researcher.py) — Phase 3 of
plans/REVIEW_INBOX_AUTONOMY_PLAN.md.

Tests the ORCHESTRATION, not the model's judgement — both the web search
and the LLM call are monkeypatched, same approach as
tests/test_review_resolver.py and for the same reason: `research_pending()`
scans the whole pending queue by design, so every call below passes
master_ids=[...] to keep a canned verdict from ever touching the real,
production pending reviews sitting alongside the test data.

What must not drift:
  - only RESEARCH_FIELDS (city, website, funding_stage, address, country,
    contact_info) are ever researched — sub_industry etc. stay Phase 2's job;
  - a citation URL that was never actually shown to the model is discarded,
    not trusted (the regional/enrich.py fabricated-citation lesson);
  - apply=True only closes "current confirmed, high confidence" — same
    asymmetry as the resolver, reusing group_may_auto_apply unchanged;
  - the search budget (resolver_max_searches) is a hard cap.
"""
import asyncio

import processing.review_researcher as review_researcher
from database.models import DuplicateReview, SuppressedMatch
from processing.review_researcher import research_pending


def _pending(db, master_id):
    return db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == "field_update",
        DuplicateReview.status == "pending",
    ).all()


def _stage(db, master_id, master_name, field, old, new):
    r = DuplicateReview(
        review_type="field_update", master_id=master_id, master_name=master_name,
        incoming_name=master_name,
        proposed_changes={field: {"old": old, "new": new, "incoming_source": "pytest"}},
        risk_level="low", status="pending", source="pytest",
    )
    db.add(r)
    db.commit()
    return r


def _fake_search(*a, **k):
    return [{"title": "Company registry", "url": "https://handelsregister.de/x",
             "snippet": "Headquartered in Hamburg since 2019."}]


# ── only the research-needed fields are touched ─────────────────────────────

def test_non_research_fields_are_ignored(make, db, monkeypatch):
    """sub_industry is Phase 2's job — the researcher must never pick it up,
    even if it's the only pending field for this master."""
    rid, _ = make("Researcher Skip", website="pytest-researcher-skip.com", city="Munich",
                  description="widget maker")
    _stage(db, rid, "PYTEST Researcher Skip", "sub_industry", "Fintech", "HealthTech")

    calls = []
    monkeypatch.setattr(review_researcher, "research_field_group",
                        lambda *a, **k: calls.append(1) or None)
    monkeypatch.setattr("ingestion.web_search.search", _fake_search)

    stats = asyncio.run(research_pending(limit=50, apply=False, master_ids=[rid]))

    assert calls == []
    assert stats.get("searches_used", 0) == 0


def test_city_field_is_researched_with_search_evidence(make, db, monkeypatch):
    rid, _ = make("Researcher City", website="pytest-researcher-city.com", city="Berlin",
                  description="widget maker")
    _stage(db, rid, "PYTEST Researcher City", "city", "Berlin", "Hamburg")

    seen = {}

    def fake_research(record, field, current, candidates, results, **kw):
        seen["field"] = field
        seen["current"] = current
        seen["candidates"] = candidates
        seen["results"] = results
        return {"winner": None, "none_fit": False, "confidence": "high",
                "reasoning": "registry confirms Berlin", "considered": candidates,
                "model": "test", "source_url": results[0]["url"], "search_results": results}

    monkeypatch.setattr(review_researcher, "research_field_group", fake_research)
    monkeypatch.setattr("ingestion.web_search.search", _fake_search)

    stats = asyncio.run(research_pending(limit=50, apply=False, master_ids=[rid]))

    assert seen["field"] == "city"
    assert seen["current"] == "Berlin"  # live value
    assert seen["candidates"] == ["Hamburg"]
    assert len(seen["results"]) == 1  # the fake search results were actually passed through
    assert stats["searches_used"] == 1

    db.expire_all()
    rev = _pending(db, rid)[0]
    assert rev.evidence.get("web_verdict", {}).get("summary") == "registry confirms Berlin"
    assert rev.evidence.get("search_results")


# ── apply only closes the reversible direction ──────────────────────────────

def test_apply_closes_confirmed_current(make, db, monkeypatch):
    rid, _ = make("Researcher Confirm", website="pytest-researcher-confirm.com", city="Berlin",
                  description="widget maker")
    _stage(db, rid, "PYTEST Researcher Confirm", "city", "Berlin", "Hamburg")

    monkeypatch.setattr(review_researcher, "research_field_group", lambda *a, **k: {
        "winner": None, "none_fit": False, "confidence": "high",
        "reasoning": "test", "considered": ["Hamburg"], "model": "test",
        "source_url": None, "search_results": []})
    monkeypatch.setattr("ingestion.web_search.search", _fake_search)

    asyncio.run(research_pending(limit=50, apply=True, master_ids=[rid]))

    db.expire_all()
    assert _pending(db, rid) == []
    sup = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "rejected_value", SuppressedMatch.master_id == rid,
        SuppressedMatch.field == "city", SuppressedMatch.value == "Hamburg",
    ).first()
    assert sup is not None


def test_apply_never_overwrites_website_even_at_high_confidence(make, db, monkeypatch):
    """website is both a research field AND an identity field — never
    auto-applied regardless of what the search evidence says."""
    rid, _ = make("Researcher Website", website="pytest-researcher-website-old.com",
                  city="Berlin", description="widget maker")
    _stage(db, rid, "PYTEST Researcher Website", "website",
          "pytest-researcher-website-old.com", "pytest-researcher-website-new.com")

    monkeypatch.setattr(review_researcher, "research_field_group", lambda *a, **k: {
        "winner": "pytest-researcher-website-new.com", "none_fit": False,
        "confidence": "high", "reasoning": "test", "considered": [], "model": "test",
        "source_url": None, "search_results": []})
    monkeypatch.setattr("ingestion.web_search.search", _fake_search)

    asyncio.run(research_pending(limit=50, apply=True, master_ids=[rid]))

    db.expire_all()
    assert len(_pending(db, rid)) == 1  # left for a human
    from database.models import Startup
    assert db.query(Startup).filter(Startup.id == rid).first().website == \
        "pytest-researcher-website-old.com"  # untouched


# ── the fabricated-citation guard ───────────────────────────────────────────

def test_fabricated_citation_is_discarded_not_trusted(make, db, monkeypatch):
    """The exact lesson from regional/enrich.py, 11 Aug: a model can cite a
    plausible-looking URL that never appeared in its own search results.
    research_field_group (the real implementation, not a fake here) must
    downgrade that to 'none' rather than accept it."""
    from processing.review_researcher import research_field_group

    class FakeMaster:
        name = "PYTEST Fabricated Citation Co"

    fake_results = [{"title": "Real result", "url": "https://real-source.example/x",
                     "snippet": "Something unrelated."}]

    def fake_chat(*a, **k):
        return {"message": {"content": (
            '{"choice": "Hamburg", "confidence": "high", '
            '"source_url": "https://fabricated-not-shown.example/y", '
            '"reasoning": "the registry says so"}'
        )}}

    class FakeClient:
        def chat(self, **kw):
            return fake_chat()

    monkeypatch.setattr("reasoning.qwen_client.qwen_client._client", lambda: FakeClient())

    res = research_field_group(FakeMaster(), "city", "Berlin", ["Hamburg"], fake_results)

    assert res is not None
    assert res["none_fit"] is True          # downgraded, not trusted
    assert res["confidence"] == "low"
    assert res["winner"] is None


# ── budget is a hard cap ─────────────────────────────────────────────────────

def test_search_budget_stops_the_run(make, db, monkeypatch):
    r1, _ = make("Researcher Budget A", website="pytest-researcher-budgeta.com",
                 city="Berlin", description="widget maker")
    r2, _ = make("Researcher Budget B", website="pytest-researcher-budgetb.com",
                 city="Berlin", description="widget maker")
    _stage(db, r1, "PYTEST Researcher Budget A", "city", "Berlin", "Hamburg")
    _stage(db, r2, "PYTEST Researcher Budget B", "city", "Berlin", "Munich")

    from config import settings
    monkeypatch.setattr(settings, "resolver_max_searches", 1)

    search_calls = []
    monkeypatch.setattr("ingestion.web_search.search",
                        lambda *a, **k: search_calls.append(1) or _fake_search())
    monkeypatch.setattr(review_researcher, "research_field_group", lambda *a, **k: {
        "winner": None, "none_fit": False, "confidence": "high",
        "reasoning": "test", "considered": [], "model": "test",
        "source_url": None, "search_results": []})

    stats = asyncio.run(research_pending(limit=50, apply=False, master_ids=[r1, r2]))

    assert len(search_calls) == 1  # budget respected — only one search made
    assert stats.get("budget_exhausted", 0) >= 1


# ── degrades cleanly when the model is unavailable ──────────────────────────

def test_consecutive_failures_stop_without_raising(make, db, monkeypatch):
    rid, _ = make("Researcher Down", website="pytest-researcher-down.com", city="Berlin",
                  description="widget maker")
    _stage(db, rid, "PYTEST Researcher Down", "city", "Berlin", "Hamburg")

    monkeypatch.setattr("ingestion.web_search.search", _fake_search)
    monkeypatch.setattr(review_researcher, "research_field_group", lambda *a, **k: None)

    stats = asyncio.run(research_pending(limit=50, apply=True, master_ids=[rid]))

    assert stats.get("unavailable", 0) >= 1
    assert "error" not in stats
    db.expire_all()
    assert len(_pending(db, rid)) == 1  # untouched
