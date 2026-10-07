"""
One-pagers from the HubDrive database (no deck): the record is only a lead;
the website, the source article and targeted searches supply the facts.
No network, no model, no database: every outside call is stubbed.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "templates" / "one_pager"))

import generate as gen    # noqa: E402
import research           # noqa: E402
import sitereader         # noqa: E402
import websearch as ws    # noqa: E402


# ── telling the company's own site apart from pages about it ─────────────────

@pytest.mark.parametrize("url,name,own", [
    ("https://bliro.io", "Bliro", True), ("https://getbliro.com", "Bliro", True),
    ("https://atira.ai/", "Atira GmbH", True), ("https://eidolamaterials.ch/", "Eidola Materials", True),
    ("https://www.hula.earth", "Hula Earth", True),
    ("https://www.munich-startup.de/news/atira-erhaelt-13-millionen", "Atira", False),
    ("https://tech.eu/2026/09/03/atira-raises", "Atira", False),
    ("https://atirahotels.com/management", "Atira", False),
])
def test_official_domain(url, name, own):
    assert sitereader.is_official_domain(url, name) is own


def test_a_namesakes_profile_page_is_dropped():
    """Searching 'Atira' returned CB Insights' page for 'Atira Hotels'."""
    hotels = {"title": "Atira Hotels - People", "url": "https://www.cbinsights.com/company/atira-hotels/people",
              "snippet": "Atira Hotels management team"}
    ours = {"title": "Atira", "url": "https://www.cbinsights.com/company/atira", "snippet": "Atira builds AI"}
    assert not ws._names_company(hotels, "Atira")
    assert ws._names_company(ours, "Atira")


def test_another_domain_on_the_same_name_is_another_company(monkeypatch):
    rep = ws.SearchReport(results=[
        {"url": "https://atira.ai/product", "title": "Atira", "snippet": "Atira"},
        {"url": "https://atirahotels.com/x", "title": "Atira Hotels", "snippet": "Atira"},
        {"url": "https://fortune.com/atira-raises", "title": "Atira raises", "snippet": "Atira"}])
    monkeypatch.setattr(ws, "_fetch", lambda u: "")
    ws.rebuild(rep, "Atira", "https://atira.ai/")
    assert [r["url"] for r in rep.results] == ["https://fortune.com/atira-raises"]


def test_third_party_pages_contribute_only_paragraphs_about_the_company():
    page = "Newsletter.\nQuilva raised 5M for robots.\nAtira raised 13M.\nIt automates RFQs.\nOther co sells shoes."
    assert sitereader.name_excerpt(page, "Atira") == "Atira raised 13M. It automates RFQs."


# ── the checklist of what a one-pager needs ──────────────────────────────────

def test_coverage_finds_what_is_there_and_what_is_not():
    text = ("Atira develops an AI platform for industrial sales. Customers include Siemens. "
            "It saves 40 % of quoting time. Founded in 2024, based in Munich. Sold as SaaS subscription.")
    c = research.coverage(text)
    assert c["product"] and c["traction"] and c["figures"] and c["founded"] and c["business_model"]
    assert c["location"]
    assert not c["competition"] and not c["team"]


def test_the_record_lead_leaves_out_founders_and_tags():
    rec = {"name": "Atira", "description": "AI for sales", "founders": ["Christian Lindner"],
           "tags": ["24 Hours"], "city": "Munich"}
    text = research.record_text(rec)
    assert "Lindner" not in text and "24 Hours" not in text and "City: Munich" in text


# ── research: free sources first, searches only for the gaps ─────────────────

@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Stub every outside call; record which searches would have run."""
    calls = []
    site = sitereader.SiteRead(url="https://atira.ai/", pages=[(
        "https://atira.ai/", "Atira develops an AI platform for industrial sales teams. Customers include "
        "Siemens and Bosch. It cuts quoting time by 40 %. Sold as a SaaS subscription. "
        "Impressum: Atira GmbH, 80331 München. Founded in 2024.")])
    monkeypatch.setattr(sitereader, "read_site", lambda url: site)
    monkeypatch.setattr(sitereader, "read_article",
                        lambda url, name: ("Atira raised 13M for AI in industrial sales.", ["https://atira.ai/"]))

    def fake_search(name, queries, website=None, *, allow_paid=True, max_paid=2, rep=None):
        rep = rep or ws.SearchReport()
        for q in queries:
            if q not in rep.queries:
                rep.queries.append(q)
                calls.append(q)
        return rep
    monkeypatch.setattr(research.websearch, "search", fake_search)
    monkeypatch.setattr(research.websearch, "all_result_urls", lambda rep: [])
    monkeypatch.setattr(research.websearch, "rebuild", lambda rep, name, website=None: None)
    return calls


def test_research_finds_the_website_free_and_searches_only_the_gaps(offline):
    rec = {"id": "x", "name": "Atira", "website": "https://www.munich-startup.de/news/atira",
           "source_url": "https://tech.eu/atira-raises", "city": "Munich"}
    res = research.run(rec, log=lambda m: None)
    assert res.website == "https://atira.ai/" and res.website_via == "a link in the source article"
    assert res.article.startswith("Atira raised")
    assert res.missing == ["competition", "team"]
    gap_queries = [q for q in offline if q != research.FACTS_QUERY.format(name="Atira")]
    assert gap_queries == ["Atira competitors alternatives", "Atira team size employees"], \
        "only the uncovered items are searched for"


def test_still_missing_items_become_open_questions(offline):
    res = research.run({"id": "x", "name": "Atira", "source_url": "https://tech.eu/a"}, log=lambda m: None)
    qs = research.missing_questions(res)
    assert any("Competitors" in q and "Your input" in q for q in qs)
    # Team size / founding year / location are asked once, field by field, by
    # generate.build_yaml — not a second time here.
    assert not any("Team size" in q for q in qs)


def test_a_team_number_must_sit_next_to_a_team_word():
    assert gen._supported_team("6", "bliro has 6 total employees") == "6"
    assert gen._supported_team("6", "spart 6 bis 8 Stunden pro Woche") is None
    assert gen._supported_team("11–50", "11-50 employees on LinkedIn") == "11–50"


# ── generate.py --record end to end ───────────────────────────────────────────

def test_record_mode_writes_both_languages_with_sources_and_research(offline, tmp_path, monkeypatch):
    import yaml
    monkeypatch.setattr(gen, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gen, "ASSETS_DIR", tmp_path / "assets")
    monkeypatch.setattr(gen, "DECKS_DIR", tmp_path / "decks")
    monkeypatch.setattr(gen.logo_fetch, "fetch_logo",
                        lambda url, out: {"found": False, "source": None, "path": None, "note": "none."})
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"id": "abc-1", "name": "Atira", "source_url": "https://tech.eu/a",
                               "city": "Munich", "description": "AI for industrial sales"}))
    monkeypatch.setattr(sys, "argv", ["generate.py", "--record", str(rec), "--no-llm"])
    assert gen.main() == 0
    de = yaml.safe_load((tmp_path / "atira.de.yaml").read_text(encoding="utf-8"))
    en = yaml.safe_load((tmp_path / "atira.en.yaml").read_text(encoding="utf-8"))
    assert de["source_record"] == {"id": "abc-1", "name": "Atira"} and en["source_record"]["id"] == "abc-1"
    assert de["website"] == "https://atira.ai/"
    assert any("HubDrive database record" in s and "lead only" in s for s in de["sources"])
    assert any("Website pages read: https://atira.ai/" in s for s in de["sources"])
    assert de["research"]["missing"] == ["competition", "team"]
    assert any("identified automatically" in q for q in de["review"]["open_questions"])


def test_deck_and_record_are_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate.py", "--name", "X"])
    assert gen.main() == 1
    assert "exactly one of --deck" in capsys.readouterr().out


# ── the batch limit in the API ────────────────────────────────────────────────

def test_the_api_refuses_more_than_max_batch():
    import asyncio
    from fastapi import HTTPException
    from api.routes import onepager as route

    body = route.FromDatabase(startup_ids=[f"id{i}" for i in range(route.MAX_BATCH + 1)])
    with pytest.raises(HTTPException) as e:
        asyncio.run(route.create_from_database(body))
    assert e.value.status_code == 422 and f"At most {route.MAX_BATCH}" in e.value.detail
