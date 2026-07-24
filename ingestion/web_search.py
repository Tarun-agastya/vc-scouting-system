"""
Backend-callable web search (Phase W, 23 Jul; Tavily primary, 24 Jul).

Phase W's plan originally assumed the WebSearch tool available in this
interactive session could back the automated verification pass — it can't;
that tool only exists for the interactive Claude Code session, not for
unattended backend Python running on the Mac mini. This module is the real,
callable replacement.

History (why Tavily, not a scraped consumer search engine):
23 Jul — shipped scraping DuckDuckGo's keyless HTML endpoint, to avoid any
  new cost/API key.
24 Jul — that endpoint started serving its anti-bot "Lite" challenge page on
  every request after a burst of ~250 queries, and was still blocked a full
  day later on two separate DuckDuckGo endpoints (an IP-level flag, not a
  narrow rate limit). Evaluated alternatives the same day: Startpage served
  no content, Mojeek served a CAPTCHA immediately, and Bing was caught doing
  something worse than blocking — serving RANDOMIZED, entirely unrelated
  decoy content on a normal 200 response (same query, two consecutive
  requests, two different sets of nonsense). That risks silently staging
  confidently-wrong "verified" facts, which is exactly what this system's
  grounding/verification machinery exists to prevent — not deployed.
  Google's Custom Search API is closed to new customers. Settled on Tavily:
  a real API built specifically for feeding LLM/agent pipelines, with a
  genuinely recurring free tier (not a one-time credit) — owner-approved
  24 Jul specifically because it avoids the scraping-arms-race failure mode
  the other options all had one version of.

DuckDuckGo scraping is kept as a fallback (tried only if Tavily is
unconfigured or its call fails) — it fails SAFELY (empty results, clearly
logged), unlike Bing's decoy-content risk, so it's a reasonable last resort
rather than a liability.

This whole module is the ONE explicit exception to the "all inference stays
local" invariant (A.1) — the search query (a public startup name) leaves the
machine; the LLM reasoning over the results still runs locally.
"""
import logging
import threading
import time
from urllib.parse import urlparse, parse_qs, unquote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# Politeness throttle, applied to the DuckDuckGo fallback only (Tavily is a
# real API with its own rate limiting — no need to self-throttle it). Kept
# here because DuckDuckGo's keyless HTML endpoint blocks a rapid burst —
# observed live 23 Jul: ~250 back-to-back queries got that endpoint blocked
# outright, and it was still blocked a full day later.
_MIN_REQUEST_INTERVAL_S = 3.0
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


class _SearchUnavailable(Exception):
    """A provider is unconfigured or its call failed — try the next one."""


# ── Tavily (primary) ─────────────────────────────────────────────────────────

_TAVILY_URL = "https://api.tavily.com/search"


def _search_tavily(query: str, max_results: int, timeout: float) -> list:
    from config import settings

    if not settings.tavily_api_key:
        raise _SearchUnavailable("TAVILY_API_KEY not configured")

    resp = httpx.post(
        _TAVILY_URL,
        headers={
            "Authorization": f"Bearer {settings.tavily_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in data.get("results", [])
        if r.get("url")
    ][:max_results]


# ── DuckDuckGo (fallback — fails safely, empty results, no decoy risk) ──────

_DDG_URL = "https://html.duckduckgo.com/html/"


def _resolve_ddg_url(href: str) -> str:
    """DuckDuckGo's HTML results wrap the real URL in /l/?uddg=<encoded>&rut=... — unwrap it."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "uddg" in qs:
        return unquote(qs["uddg"][0])
    return href


def _search_duckduckgo(query: str, max_results: int, timeout: float) -> list:
    _throttle()
    resp = httpx.get(_DDG_URL, params={"q": query}, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    # A non-200 "success" (raise_for_status only rejects 4xx/5xx) is DuckDuckGo's
    # soft anti-bot signal — a 202 response serves its "Lite" challenge page
    # instead of real results, with no exception and no /html/ result markup
    # to parse. Treated as unavailable rather than "no results found".
    if resp.status_code != 200:
        raise _SearchUnavailable(f"HTTP {resp.status_code} (likely an anti-bot challenge)")

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for result in soup.select("div.result"):
        title_el = result.select_one("a.result__a")
        snippet_el = result.select_one("a.result__snippet")
        if not title_el:
            continue
        url = _resolve_ddg_url(title_el.get("href", ""))
        if not url:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url": url,
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
        if len(results) >= max_results:
            break
    return results


_PROVIDERS = [("tavily", _search_tavily), ("duckduckgo", _search_duckduckgo)]


def search(query: str, max_results: int = 5, timeout: float = 15.0) -> list:
    """
    Query the web via whichever provider actually works, in order (Tavily,
    then DuckDuckGo as fallback). Synchronous (matches
    reasoning/qwen_client.py's sync-client pattern; the caller dispatches it
    off the event loop via run_in_executor, same as every Ollama call).

    Returns [{"title": str, "url": str, "snippet": str}, ...], best-effort —
    empty list only if every provider is unavailable or fails, rather than
    raising, so a search hiccup never crashes a verification batch.
    """
    for name, fn in _PROVIDERS:
        try:
            return fn(query, max_results, timeout)
        except Exception as exc:
            logger.warning(f"[WebSearch] {name} unavailable for {query!r}: {exc}")

    logger.warning(f"[WebSearch] All providers failed for {query!r}")
    return []
