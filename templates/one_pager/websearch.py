"""
Web search for the one-pager generator — with the Tavily credits protected.

WHY THIS IS ITS OWN SMALL CLIENT. The pipeline has ingestion/web_search.py, but
the one-pager tool may not import pipeline code (FORMAT.md §7). This is ~200
lines of httpx instead, with stricter spending rules than the pipeline's.

WHERE RESULTS COME FROM, cheapest first:
  1. cache       — the same query in the last CACHE_DAYS days costs nothing again,
                   so regenerating a draft with "overwrite" never pays twice;
  2. SearXNG     — self-hosted, free, no quota (docker-compose `searxng`);
  3. Tavily      — paid credits (1 per basic search), ONLY when SearXNG gave nothing.

HOW THE CREDITS ARE PROTECTED. The Tavily plan (1,000 credits/month) is shared
with the whole scouting pipeline, which has no quota accounting of its own. So
before EVERY paid search this checks the account's real balance via Tavily's
/usage endpoint (free to call) and refuses when:
  * fewer than TAVILY_RESERVE credits would remain — the pipeline's share,
    which the one-pager may never touch;
  * this one-pager already used MAX_SEARCHES_PER_DRAFT searches;
  * the one-pager tool used MONTHLY_CAP credits this month (local ledger);
  * the balance can't be read at all — unknown means no, never "probably fine".
Every refusal is reported, never silent, and drafting continues without it.

WHAT IS KEPT. Only results that name the company (title or snippet) — a search
for a short name also returns other companies with the same name, which is how
facts end up on the wrong startup (the pipeline's cross-attribution bug).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / ".websearch"                  # cache + ledger; gitignored
LEDGER = STATE_DIR / "ledger.json"
CACHE_DIR = STATE_DIR / "cache"

SEARXNG_URL = os.environ.get("ONEPAGER_SEARXNG_URL", "http://localhost:8888").rstrip("/")
TAVILY_SEARCH = "https://api.tavily.com/search"
TAVILY_USAGE = "https://api.tavily.com/usage"

MAX_SEARCHES_PER_DRAFT = int(os.environ.get("ONEPAGER_MAX_SEARCHES", "2"))
MONTHLY_CAP = int(os.environ.get("ONEPAGER_MONTHLY_SEARCH_CAP", "60"))
TAVILY_RESERVE = int(os.environ.get("ONEPAGER_TAVILY_RESERVE", "250"))
CACHE_DAYS = 30
RESULTS_PER_QUERY = 5
WEB_TEXT_BUDGET = 4000            # characters of web material handed to the model
PAGE_FETCH = 2                    # third-party pages read in full (free: plain HTTP)


@dataclass
class SearchReport:
    """What happened, for the YAML's sources/open_questions and the log."""
    results: List[dict] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    paid: int = 0                 # Tavily credits spent by this draft
    cached: int = 0
    free: int = 0
    notes: List[str] = field(default_factory=list)
    text: str = ""                # the material handed to the model

    @property
    def urls(self) -> List[str]:
        return [r["url"] for r in self.results]


# ── Key + ledger ─────────────────────────────────────────────────────────────

def _tavily_key() -> str:
    """From the environment (the API passes it), else the repo's .env for CLI runs.
    Read as a file, never imported — and never logged or printed."""
    key = os.environ.get("TAVILY_API_KEY") or os.environ.get("tavily_api_key")
    if key:
        return key.strip()
    env = HERE.parent.parent / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"(?i)^\s*tavily_api_key\s*=\s*(.*)$", line)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        pass
    return ""


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


def ledger() -> dict:
    """{"2026-10": {"credits": 6, "drafts": 3}, ...}"""
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _charge(credits: int) -> None:
    data = ledger()
    m = data.setdefault(_month(), {"credits": 0, "searches": 0})
    m["credits"] += credits
    m["searches"] += 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(data, indent=1), encoding="utf-8")


def used_this_month() -> int:
    return int(ledger().get(_month(), {}).get("credits", 0))


def tavily_balance() -> Optional[dict]:
    """{"used", "limit", "left"} for the whole Tavily plan, or None if unknown."""
    key = _tavily_key()
    if not key:
        return None
    try:
        import httpx

        r = httpx.get(TAVILY_USAGE, headers={"Authorization": f"Bearer {key}"}, timeout=10)
        r.raise_for_status()
        acct = r.json().get("account") or {}
        used, limit = acct.get("plan_usage"), acct.get("plan_limit")
        if used is None or not limit:
            return None
        return {"used": int(used), "limit": int(limit), "left": int(limit) - int(used)}
    except Exception as exc:
        logger.warning(f"[websearch] Tavily balance unavailable: {type(exc).__name__}")
        return None


def budget_status() -> dict:
    """For the dashboard: what a search would be allowed to spend right now."""
    bal = tavily_balance()
    left_for_onepager = None
    if bal:
        left_for_onepager = max(0, min(bal["left"] - TAVILY_RESERVE, MONTHLY_CAP - used_this_month()))
    return {
        "tavily": bal,
        "reserve": TAVILY_RESERVE,
        "onepager_used_this_month": used_this_month(),
        "onepager_monthly_cap": MONTHLY_CAP,
        "per_draft": MAX_SEARCHES_PER_DRAFT,
        "paid_searches_available": left_for_onepager,
        "searxng_ok": _searxng_ok(),
    }


def _may_spend(report: SearchReport) -> Optional[str]:
    """None if one more paid search is allowed, else the human reason why not."""
    if report.paid >= MAX_SEARCHES_PER_DRAFT:
        return f"per-draft limit of {MAX_SEARCHES_PER_DRAFT} paid searches reached"
    if used_this_month() >= MONTHLY_CAP:
        return f"the one-pager's monthly limit of {MONTHLY_CAP} Tavily credits is used up"
    if not _tavily_key():
        return "no Tavily key configured"
    bal = tavily_balance()
    if bal is None:
        return "Tavily's remaining credits could not be checked, so none were spent"
    if bal["left"] - 1 < TAVILY_RESERVE:
        return (f"only {bal['left']} Tavily credits left this month; the last "
                f"{TAVILY_RESERVE} are reserved for the scouting pipeline")
    return None


# ── Cache ────────────────────────────────────────────────────────────────────

def _cache_path(query: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(query.lower().encode()).hexdigest()[:16] + ".json")


def _cache_get(query: str) -> Optional[list]:
    p = _cache_path(query)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if datetime.fromisoformat(data["at"]) > datetime.now() - timedelta(days=CACHE_DAYS):
            return data["results"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cache_put(query: str, results: list, provider: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(query).write_text(json.dumps(
        {"query": query, "at": datetime.now().isoformat(timespec="seconds"),
         "provider": provider, "results": results}, ensure_ascii=False), encoding="utf-8")


# ── Providers ────────────────────────────────────────────────────────────────

def _searxng_ok() -> bool:
    try:
        import httpx

        return httpx.get(f"{SEARXNG_URL}/healthz", timeout=3).status_code == 200
    except Exception:
        return False


def _searxng(query: str) -> list:
    try:
        import httpx

        r = httpx.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                for x in r.json().get("results", []) if x.get("url")][:RESULTS_PER_QUERY]
    except Exception as exc:
        logger.info(f"[websearch] SearXNG unavailable ({type(exc).__name__})")
        return []


def _tavily(query: str, exclude: Optional[str] = None) -> Optional[list]:
    """None = the call failed (nothing charged locally). [] = no results (charged)."""
    try:
        import httpx

        r = httpx.post(TAVILY_SEARCH, timeout=20,
                       headers={"Authorization": f"Bearer {_tavily_key()}"},
                       json={"query": query, "max_results": RESULTS_PER_QUERY,
                             "search_depth": "basic",           # basic = 1 credit
                             # The company's own site is read separately; on the
                             # first live test it took 2 of the 5 paid slots.
                             **({"exclude_domains": [exclude]} if exclude else {})})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                for x in r.json().get("results", []) if x.get("url")][:RESULTS_PER_QUERY]
    except Exception as exc:
        logger.warning(f"[websearch] Tavily call failed ({type(exc).__name__})")
        return None


# ── Search for one startup ───────────────────────────────────────────────────

def _domain(url: str) -> str:
    d = urlparse(url if "//" in url else f"//{url}").netloc.lower()
    return d[4:] if d.startswith("www.") else d


def _names_company(result: dict, name: str) -> bool:
    """The company's name must appear as a word in the title or snippet."""
    hay = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    return re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", hay) is not None


def queries_for(name: str) -> List[str]:
    """Two questions the deck most often leaves open: the company facts (city,
    founding, team) and outside evidence (funding, customers, press). Plain
    phrases: German keyword strings inside a quoted name returned mostly noise
    on the first live test (Eidola, Oct 2026)."""
    return [
        f"{name} startup founded founders location",
        f"{name} startup funding customers news",
    ][:MAX_SEARCHES_PER_DRAFT]


def search_company(name: str, website: Optional[str] = None, *, allow_paid: bool = True) -> SearchReport:
    """
    Run the startup's queries through cache -> SearXNG -> Tavily (guarded), keep
    results that name the company, read the best two pages, and return one
    text block for the model plus a report of what was spent. Never raises.
    """
    rep = SearchReport()
    own = _domain(website) if website else ""
    seen, kept = set(), []

    for q in queries_for(name):
        rep.queries.append(q)
        results = _cache_get(q)
        if results is not None:
            rep.cached += 1
        else:
            results = _searxng(q)
            if results:
                rep.free += 1
                _cache_put(q, results, "searxng")
            elif allow_paid:
                why_not = _may_spend(rep)
                if why_not:
                    rep.notes.append(f"Paid web search skipped: {why_not}.")
                    results = []
                else:
                    results = _tavily(q, exclude=own or None)
                    if results is None:
                        rep.notes.append("Tavily search failed; continuing without it.")
                        results = []
                    else:
                        rep.paid += 1
                        _charge(1)
                        _cache_put(q, results, "tavily")
            else:
                rep.notes.append("Paid web search was switched off for this draft.")
                results = []

        for r in results:
            u = r["url"].split("#")[0]
            if u in seen or (own and _domain(u) == own) or not _names_company(r, name):
                continue
            seen.add(u)
            kept.append(r)

    rep.results = kept
    rep.text = _build_text(kept)
    # Dedupe notes (two queries can hit the same refusal).
    rep.notes = list(dict.fromkeys(rep.notes))
    return rep


def _build_text(results: List[dict]) -> str:
    """Snippets for all kept results, plus the full text of the first PAGE_FETCH
    pages (plain HTTP, costs no credits), capped at WEB_TEXT_BUDGET."""
    blocks, used = [], 0
    for i, r in enumerate(results):
        body = r.get("snippet", "")
        if i < PAGE_FETCH:
            page = _fetch(r["url"])
            if page:
                body = page
        block = f"[Quelle: {r['url']}]\n{' '.join(body.split())}"
        room = WEB_TEXT_BUDGET - used
        if room < 200:
            break
        blocks.append(block[:room])
        used += len(blocks[-1]) + 2
    return "\n\n".join(blocks)


def _fetch(url: str) -> str:
    try:
        import trafilatura

        dl = trafilatura.fetch_url(url)
        return (trafilatura.extract(dl) or "")[:1800] if dl else ""
    except Exception:
        return ""


if __name__ == "__main__":       # python3 templates/one_pager/websearch.py  -> budget status
    print(json.dumps(budget_status(), indent=1))
