"""
ingestion/web_search.py — provider chain and the SearXNG fallback (11 Aug 2026).

No real network calls: httpx.get/post are monkeypatched with a small fake
response, so these run offline and don't depend on Docker/SearXNG actually
being up (the live container is verified separately, by hand, against the
real docker-compose service).
"""
import httpx
import pytest

from ingestion import web_search


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


# ── provider order ───────────────────────────────────────────────────────────

def test_provider_order_is_tavily_then_searxng_then_duckduckgo():
    """SearXNG sits between Tavily and DuckDuckGo deliberately — it's the
    more robust free option, placed ahead of the fallback (DuckDuckGo's own
    scrape) that failed first and is why Tavily was added in the first
    place."""
    names = [name for name, _ in web_search._PROVIDERS]
    assert names == ["tavily", "searxng", "duckduckgo"]


# ── SearXNG result parsing ───────────────────────────────────────────────────

def test_searxng_maps_content_field_to_snippet(monkeypatch):
    """SearXNG's JSON schema calls the field 'content', not 'snippet' like
    Tavily/DuckDuckGo — this is the one real translation this function does."""
    def fake_get(url, params=None, timeout=None):
        assert "/search" in url
        assert params["format"] == "json"
        return _FakeResponse({"results": [
            {"title": "Acme GmbH", "url": "https://acme.de", "content": "A widget maker."},
        ]})

    monkeypatch.setattr(web_search.httpx, "get", fake_get)
    out = web_search._search_searxng("Acme GmbH Memmingen", 5, 15.0)
    assert out == [{"title": "Acme GmbH", "url": "https://acme.de", "snippet": "A widget maker."}]


def test_searxng_drops_results_with_no_url(monkeypatch):
    monkeypatch.setattr(web_search.httpx, "get", lambda *a, **k: _FakeResponse(
        {"results": [{"title": "no url", "content": "x"}, {"title": "ok", "url": "https://x.de"}]}))
    out = web_search._search_searxng("q", 5, 15.0)
    assert len(out) == 1 and out[0]["url"] == "https://x.de"


def test_searxng_respects_max_results(monkeypatch):
    many = [{"title": str(i), "url": f"https://x{i}.de"} for i in range(10)]
    monkeypatch.setattr(web_search.httpx, "get", lambda *a, **k: _FakeResponse({"results": many}))
    assert len(web_search._search_searxng("q", 3, 15.0)) == 3


def test_searxng_bad_status_raises_and_is_caught_by_the_chain(monkeypatch):
    """A 403 (JSON format not enabled) or a container-down error must fall
    through to the next provider, never crash the caller."""
    def fake_get(*a, **k):
        return _FakeResponse(status_code=403)
    monkeypatch.setattr(web_search.httpx, "get", fake_get)
    with pytest.raises(Exception):
        web_search._search_searxng("q", 5, 15.0)


# ── fallthrough behaviour of search() ───────────────────────────────────────

def test_search_falls_through_tavily_to_searxng(monkeypatch):
    """No Tavily key configured -> SearXNG should be tried next and its
    result returned, without ever reaching DuckDuckGo."""
    from config import settings
    monkeypatch.setattr(settings, "tavily_api_key", None)

    calls = []
    def fake_searxng(query, max_results, timeout):
        calls.append("searxng")
        return [{"title": "t", "url": "https://x.de", "snippet": "s"}]
    def fake_ddg(*a, **k):
        calls.append("duckduckgo")
        raise AssertionError("should not reach DuckDuckGo")

    monkeypatch.setattr(web_search, "_search_searxng", fake_searxng)
    monkeypatch.setattr(web_search, "_search_duckduckgo", fake_ddg)
    monkeypatch.setattr(web_search, "_PROVIDERS", [
        ("tavily", web_search._search_tavily),
        ("searxng", fake_searxng),
        ("duckduckgo", fake_ddg),
    ])

    out = web_search.search("Acme GmbH")
    assert calls == ["searxng"]
    assert out and out[0]["url"] == "https://x.de"


def test_search_falls_through_all_three_to_empty_list(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(web_search, "_PROVIDERS", [
        ("tavily", boom), ("searxng", boom), ("duckduckgo", boom)])
    assert web_search.search("Acme GmbH") == []
