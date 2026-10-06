"""
The one-pager's web search must never run the shared Tavily credits out.
No network here: every provider and the balance check are stubbed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "templates" / "one_pager"))

import websearch as ws   # noqa: E402

HIT = {"title": "Acme Robotics raises seed", "url": "https://news.example/acme",
       "snippet": "Acme Robotics, founded 2021 in Ulm, raised 2 million."}
OTHER = {"title": "Acme Corp", "url": "https://other.example/x", "snippet": "Acme Corporation sells anvils."}
OWN = {"title": "Acme Robotics", "url": "https://www.acme-robotics.de/about", "snippet": "Acme Robotics home"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated ledger/cache; SearXNG empty; Tavily stubbed and counted."""
    monkeypatch.setattr(ws, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ws, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(ws, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ws, "_searxng", lambda q: [])
    monkeypatch.setattr(ws, "_fetch", lambda u: "")
    monkeypatch.setattr(ws, "_tavily_key", lambda: "k")
    calls = []
    monkeypatch.setattr(ws, "_tavily", lambda q, exclude=None: calls.append((q, exclude)) or [HIT, OTHER, OWN])
    monkeypatch.setattr(ws, "tavily_balance", lambda: {"used": 100, "limit": 1000, "left": 900})
    return calls


def test_paid_search_is_counted_and_capped_per_draft(env):
    rep = ws.search_company("Acme Robotics", "https://acme-robotics.de")
    assert rep.paid == len(env) == ws.MAX_SEARCHES_PER_DRAFT
    assert ws.used_this_month() == rep.paid


def test_regenerating_costs_nothing_thanks_to_the_cache(env):
    ws.search_company("Acme Robotics")
    before = len(env)
    rep = ws.search_company("Acme Robotics")
    assert len(env) == before and rep.paid == 0 and rep.cached == ws.MAX_SEARCHES_PER_DRAFT


def test_free_results_mean_no_paid_search(env, monkeypatch):
    monkeypatch.setattr(ws, "_searxng", lambda q: [HIT])
    rep = ws.search_company("Acme Robotics")
    assert env == [] and rep.paid == 0 and rep.free == ws.MAX_SEARCHES_PER_DRAFT


def test_the_pipeline_reserve_is_never_touched(env, monkeypatch):
    monkeypatch.setattr(ws, "tavily_balance",
                        lambda: {"used": 750, "limit": 1000, "left": ws.TAVILY_RESERVE})
    rep = ws.search_company("Acme Robotics")
    assert env == [] and rep.paid == 0
    assert any("reserved for the scouting pipeline" in n for n in rep.notes)


def test_unknown_balance_means_no_spending(env, monkeypatch):
    monkeypatch.setattr(ws, "tavily_balance", lambda: None)
    rep = ws.search_company("Acme Robotics")
    assert env == [] and any("could not be checked" in n for n in rep.notes)


def test_monthly_cap_stops_spending(env, monkeypatch):
    monkeypatch.setattr(ws, "used_this_month", lambda: ws.MONTHLY_CAP)
    rep = ws.search_company("Acme Robotics")
    assert env == [] and any("monthly limit" in n for n in rep.notes)


def test_paid_search_can_be_switched_off(env):
    rep = ws.search_company("Acme Robotics", allow_paid=False)
    assert env == [] and rep.paid == 0


def test_a_failed_tavily_call_is_not_charged(env, monkeypatch):
    monkeypatch.setattr(ws, "_tavily", lambda q, exclude=None: None)
    rep = ws.search_company("Acme Robotics")
    assert rep.paid == 0 and ws.used_this_month() == 0


def test_only_results_naming_the_company_are_kept_and_own_site_is_excluded(env):
    rep = ws.search_company("Acme Robotics", "https://www.acme-robotics.de")
    assert rep.urls == ["https://news.example/acme"], "other 'Acme's and the own site must be dropped"
    assert all(ex == "acme-robotics.de" for _, ex in env), "Tavily must be told to skip the own site"


def test_a_namesake_is_not_the_company():
    """'Studio Eidola, founded 2020' is a sister company; Eidola Materials was
    founded 2025. Keeping it would have put the wrong year on the page."""
    sister = {"title": "syntheticgeologies", "snippet": "Studio Eidola, founded in 2020 in Zürich"}
    assert not ws._names_company(sister, "Eidola Materials")


def test_search_never_raises_when_everything_is_down(env, monkeypatch):
    monkeypatch.setattr(ws, "tavily_balance", lambda: None)
    monkeypatch.setattr(ws, "_tavily", lambda q, exclude=None: None)
    rep = ws.search_company("Acme Robotics")
    assert rep.results == [] and rep.text == "" and rep.paid == 0
