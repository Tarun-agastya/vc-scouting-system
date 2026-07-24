import asyncio
import logging
from collections import deque
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Shared browser-like headers to reduce bot-detection false positives
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Below this many extracted characters, a "successful" (HTTP 200) static
# fetch is treated as an empty JS-rendering shell rather than a real page —
# React/Vue/etc. sites often return a 200 with a near-empty <body> and load
# all content client-side, which a plain httpx GET can never see.
_MIN_STATIC_TEXT_LEN = 200

# Button/link text that reveals more of a paginated directory list. Matched
# case-insensitively as a substring by Playwright's :has-text(). Bilingual
# (DE/EN) since most sources here are German. Ordered most- to least-specific.
_LOAD_MORE_PATTERNS = (
    "Mehr laden", "Mehr anzeigen", "Weitere laden", "Mehr Ergebnisse",
    "Alle anzeigen", "Load more", "Show more", "See more", "View more",
)

# Cookie-consent accept buttons. Some sites render their content grid behind
# (or with clicks blocked by) a consent overlay until it's dismissed —
# confirmed live 24 Jul: zollhof.de's portfolio grid didn't visibly change
# without this, though the real gap there was the alt-text issue above; still
# cheap, safe, and worth doing before reading/paginating ANY rendered page.
_COOKIE_ACCEPT_PATTERNS = (
    "Allow and continue", "Accept all", "Accept All", "Accept",
    "Alle akzeptieren", "Akzeptieren", "Zustimmen", "Ich stimme zu", "OK",
)

# URL path segments that mark a page as high-value startup content (a
# portfolio, a company/startup profile, an alumni/cohort list) so the crawl
# frontier visits them BEFORE generic section/nav pages within its page
# budget. Matched at the path-segment level, like SKIP_PATTERNS.
_PRIORITY_PATTERNS: frozenset = frozenset({
    "startup", "startups", "portfolio", "portfolios", "company", "companies",
    "unternehmen", "founders", "gruender", "alumni", "batch", "cohort",
    "ventures", "scaleup", "scaleups", "incubation", "members", "member",
})


def _url_priority(url: str) -> int:
    """0 = high-value (startup/portfolio/company page), 1 = everything else."""
    path_parts = urlparse(url).path.lower().strip("/").split("/")
    return 0 if any(p in _PRIORITY_PATTERNS for p in path_parts if p) else 1


# ── URL Skip Patterns ─────────────────────────────────────────────────────────
# Path segments that indicate pages with no startup intelligence value.
# Checked at the segment level so '/login' is skipped but '/online-platform'
# and '/innovation' are not false-positives.
# Add entries here to expand coverage without touching any other code.
SKIP_PATTERNS: frozenset = frozenset({
    # Authentication / account management
    "login", "logout", "signin", "signup", "register",
    "auth", "oauth", "sso", "password", "reset-password", "forgot-password",
    # Administrative
    "admin", "intranet", "dashboard", "backend", "cms",
    # Recruitment  (irrelevant to startup discovery)
    "jobs", "karriere", "career", "careers",
    "stellenangebote", "stellenangebot", "bewerbung", "apply", "hiring",
    # Legal / compliance
    "privacy", "datenschutz", "impressum", "legal",
    "terms", "agb", "cookie", "cookies", "gdpr",
    # Contact / generic navigation
    "contact", "kontakt", "support", "help", "faq",
    "newsletter", "subscribe", "unsubscribe",
    # Press / media  (usually about the org, not startups)
    "press", "media",
    # User account areas
    "profile", "account", "settings", "preferences",
    # Utility
    "search", "sitemap",
})


# ── URL Utilities ─────────────────────────────────────────────────────────────

def _base_domain(url: str) -> str:
    """Return the lowercase netloc of a URL (used for domain-isolation checks)."""
    return urlparse(url).netloc.lower()


# Generic UI/icon alt text to drop when harvesting <img alt="..."> names —
# exact match only (not substring), so a real company name that happens to
# contain one of these as part of a longer phrase is never dropped.
_ALT_NOISE = frozenset({
    "logo", "icon", "arrow", "close", "menu", "search", "chevron", "banner",
    "facebook", "twitter", "instagram", "linkedin", "youtube", "tiktok",
    "avatar", "placeholder", "background", "hero", "ok",
})


def _extract_text(html: str) -> str:
    """
    Strip boilerplate tags and return clean plain text from raw HTML.

    Also harvests meaningful <img alt="..."> values as a separate trailing
    block. Portfolio/logo-grid pages often show each company as ONLY a logo
    image, with the name living in alt text and NEVER appearing as visible
    text — confirmed live 24 Jul on zollhof.de's startup portfolio page:
    120 company names existed solely as img alt text; plain get_text()
    returned page chrome (category filter chips, "New!" badges) and missed
    every single one. Noise (generic icon/social alt text) is dropped by
    exact match; real company names always pass through untouched.
    """
    soup = BeautifulSoup(html, "html.parser")

    alts: list = []
    seen_alt: set = set()
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        key = alt.lower()
        if len(alt) < 2 or key in seen_alt or key in _ALT_NOISE:
            continue
        seen_alt.add(key)
        alts.append(alt)

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)

    if alts:
        text += "\n\nPortfolio / logo grid entries on this page:\n" + "\n".join(alts)

    return text


def _extract_links(html: str, base_url: str) -> list:
    """Return a deduplicated list of absolute URLs from all <a href> tags."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    links: list = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        # Skip non-navigable schemes
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        # Normalise: drop fragment so #section variants aren't re-visited
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
    return links


def _is_irrelevant_url(url: str) -> bool:
    """
    Return True if any path segment of *url* matches a SKIP_PATTERNS entry.

    Segment-level matching prevents false positives:
      /login           → skipped   (segment 'login' is in SKIP_PATTERNS)
      /online-platform → kept      ('online-platform' is not in SKIP_PATTERNS)
      /portfolio/founders → kept   (neither segment matches)
    """
    path_parts = urlparse(url).path.lower().strip("/").split("/")
    return any(part in SKIP_PATTERNS for part in path_parts if part)


# ── Scraper ───────────────────────────────────────────────────────────────────

class WebScraper:
    """
    Scrapes startup source pages (accelerators, incubators, university hubs).

    `scrape_source` performs an async BFS deep-crawl bounded by `max_depth`
    and `max_pages`, aggregates the text of every visited page into one context
    block, then forwards it to Qwen for startup extraction.
    """

    def __init__(self):
        self._http_timeout = httpx.Timeout(15.0, connect=5.0)

    async def scrape_source(
        self,
        url: str,
        source_type: str = "general",
        max_depth: Optional[int] = None,
        max_pages: Optional[int] = None,
        *,
        force_render: bool = False,
        validation_session=None,
        metrics=None,
        # url_priority_map: dict = None  # extension point: future priority crawling
    ):
        """
        Coordinator: launches the 4-stage worker pipeline and awaits completion.

        Stages
        ------
        Crawler Task  →  page_queue  →  Chunker Task  →  chunk_queue
          →  Qwen Worker(s)  →  storage_queue  →  Storage Worker

        Scraping and Qwen extraction run concurrently.  Back-pressure from
        bounded queues prevents memory explosions when Qwen falls behind.

        metrics : optional pre-created PipelineMetrics. Pass one in when a
          caller (e.g. ScoutController, for live dashboard progress) needs to
          read counters WHILE the run is in flight, not just after it returns.
          When omitted, a fresh one is created — unchanged default behaviour.

        Returns PipelineMetrics for the completed run (callers may ignore it).
        """
        import time
        from config import settings
        from ingestion.worker_queue import (
            PipelineMetrics,
            chunker_task,
            qwen_worker_task,
            storage_worker_task,
        )

        metrics = metrics if metrics is not None else PipelineMetrics()
        # Fall back to the configured crawl reach when the caller doesn't
        # override (the validation harness passes explicit values).
        if max_depth is None:
            max_depth = settings.crawl_max_depth
        if max_pages is None:
            max_pages = settings.crawl_max_pages
        t0 = time.time()
        num_workers = settings.max_qwen_workers

        page_queue    = asyncio.Queue(maxsize=settings.page_queue_size)
        chunk_queue   = asyncio.Queue(maxsize=settings.chunk_queue_size)
        storage_queue = asyncio.Queue(maxsize=settings.storage_queue_size)

        await asyncio.gather(
            self._crawler_task(
                url, source_type, max_depth, max_pages, page_queue, metrics,
                force_render=force_render,
            ),
            chunker_task(page_queue, chunk_queue, metrics),
            *[
                qwen_worker_task(
                    chunk_queue, storage_queue, metrics, i,
                    validation_session=validation_session,
                )
                for i in range(num_workers)
            ],
            storage_worker_task(
                storage_queue, metrics, num_workers,
                validation_session=validation_session,
            ),
        )

        metrics.total_processing_time = time.time() - t0
        metrics.report(url)
        return metrics

    # ── BFS Crawler Task ──────────────────────────────────────────────────────

    async def _crawler_task(
        self,
        start_url: str,
        source_type: str,
        max_depth: int,
        max_pages: int,
        page_queue: asyncio.Queue,
        metrics: "PipelineMetrics",
        *,
        force_render: bool = False,
    ) -> None:
        """
        Asynchronous BFS crawler — Stage 1 of the worker pipeline.

        Fetches pages and puts PageItems into page_queue as each page is
        retrieved, so the chunker and Qwen workers can start processing
        immediately without waiting for the entire crawl to complete.

        Puts the None sentinel into page_queue when the BFS is exhausted.
        Domain isolation, SKIP_PATTERNS filtering, and Playwright fallback
        are all preserved from the original _deep_crawl implementation.
        """
        from ingestion.worker_queue import PageItem

        allowed_domain = _base_domain(start_url)
        visited: set = set()
        queued: set = {start_url}   # everything ever enqueued (dedupe the frontier)
        # Two-tier priority frontier: high-value startup/portfolio pages
        # (_url_priority == 0) are drained BEFORE generic section/nav pages, so
        # the page budget reaches actual startup content instead of being spent
        # on "about / events / news" first. FIFO within each tier keeps the
        # crawl breadth-first and stable.
        frontier_high: deque = deque()
        frontier_low: deque = deque([(start_url, 0)])

        def _next():
            if frontier_high:
                return frontier_high.popleft()
            if frontier_low:
                return frontier_low.popleft()
            return None

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=self._http_timeout,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            while (frontier_high or frontier_low) and len(visited) < max_pages:
                current_url, depth = _next()

                if current_url in visited:
                    continue
                visited.add(current_url)

                html = await self._fetch_page(client, current_url, force_render=force_render)
                if not html:
                    metrics.inc("pages_skipped")
                    continue

                text = _extract_text(html)
                if text:
                    await page_queue.put(PageItem(
                        url=current_url,
                        text=text,
                        source_type=source_type,
                        source_url=start_url,
                    ))
                    metrics.inc("pages_crawled")
                    logger.debug(
                        f"[Scraper] [{depth}] Crawled {current_url} "
                        f"({len(text)} chars)"
                    )
                else:
                    metrics.inc("pages_skipped")

                # Only enqueue children if we haven't reached max depth
                if depth < max_depth:
                    for link in _extract_links(html, current_url):
                        if link in queued or _base_domain(link) != allowed_domain:
                            continue
                        if _is_irrelevant_url(link):
                            logger.debug(f"[Scraper] Skipping irrelevant URL: {link}")
                            continue
                        queued.add(link)
                        item = (link, depth + 1)
                        if _url_priority(link) == 0:
                            frontier_high.append(item)
                        else:
                            frontier_low.append(item)

        logger.info(
            f"[Scraper] Crawl complete — {metrics.pages_crawled} pages "
            f"scraped from {allowed_domain}"
        )
        await page_queue.put(None)  # sentinel: signals chunker that crawl is done

    # ── Fetch Strategies ──────────────────────────────────────────────────────

    async def _fetch_page(self, client: httpx.AsyncClient, url: str, *,
                          force_render: bool = False) -> str:
        """
        Fetch a single page.

        force_render=True (source render_mode "always"): skip the static fetch
        entirely and render in a headless browser — for JS directory sites
        (React/Vue/Next) whose content is invisible to a plain fetch. See
        WebSourceEntry.render_mode.

        Otherwise (render_mode "auto"): fast static httpx fetch, falling back to
        Playwright only when the static fetch fails outright OR "succeeds" with a
        200 but yields an empty client-side-rendering shell (< _MIN_STATIC_TEXT_LEN).
        """
        if force_render:
            # A render_mode="always" source is a JS directory — render AND
            # exhaust its pagination so we get the full list, not page one.
            return await self._fetch_playwright(url, paginate=True)

        try:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "text/html" in content_type:
                html = resp.text
                if len(_extract_text(html)) >= _MIN_STATIC_TEXT_LEN:
                    return html
                logger.debug(
                    f"[Scraper] {url} — static fetch too thin "
                    f"({len(_extract_text(html))} chars), trying Playwright"
                )
            else:
                logger.debug(
                    f"[Scraper] Skipped {url} — "
                    f"status={resp.status_code} content-type={content_type}"
                )
        except Exception as exc:
            logger.debug(f"[Scraper] httpx failed for {url}: {exc}")

        # Playwright fallback for JS-gated / Cloudflare-protected / SPA sites
        return await self._fetch_playwright(url)

    async def _fetch_playwright(self, url: str, *, paginate: bool = False) -> str:
        """
        JavaScript-aware fetch using Playwright (headless Chromium).

        Waits for the network to settle (so a client-side-loaded startup grid
        actually appears) plus a short render beat, then snapshots the DOM.
        The networkidle wait is bounded and best-effort: some sites never go
        idle (perpetual analytics/chat/websocket traffic), so a timeout there
        is expected and we proceed to snapshot whatever has rendered rather
        than failing — measured on munich-startup.de, this lifts a directory
        page from a 1.5 KB shell to ~11 KB of real content.

        paginate=True: after the first render, exhaust the list by repeatedly
        clicking a "load more" button (or scrolling for infinite-scroll lists)
        until it stops growing or the configured cap is hit — measured on
        munich-startup.de, this lifts a directory from ~11 KB (first page,
        ~12 startups) to ~350 KB (the full list). Bounded and self-stopping,
        so a non-paginated page costs at most one extra scroll.
        """
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_extra_http_headers({"User-Agent": _HEADERS["User-Agent"]})
                # domcontentloaded first (fast, always resolves), THEN try to
                # let async data-loading settle — don't hang forever on it.
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass  # never went idle — snapshot what rendered anyway
                await page.wait_for_timeout(1_500)  # brief beat for final paint
                await self._dismiss_cookie_banner(page)

                if paginate:
                    await self._exhaust_pagination(page, url)

                html = await page.content()
                await browser.close()
            return html
        except Exception as exc:
            logger.error(f"[Scraper] Playwright failed {url}: {exc}")
        return ""

    async def _dismiss_cookie_banner(self, page) -> None:
        """
        Best-effort: click a cookie-consent accept button if one is visible.
        Never raises — a banner that isn't found, or doesn't dismiss cleanly,
        just leaves the page as it was; this only ever helps, never blocks.
        """
        for pat in _COOKIE_ACCEPT_PATTERNS:
            loc = page.locator(f'button:has-text("{pat}")').first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2_000)
                    await page.wait_for_timeout(800)
                    return
            except Exception:
                continue

    async def _exhaust_pagination(self, page, url: str) -> None:
        """
        Repeatedly reveal more of a paginated list: click a "load more" button
        if present, else scroll to the bottom (infinite scroll). Stop when the
        page height stops growing for two consecutive rounds, or the configured
        click cap is reached. Fully bounded — never loops forever, and does
        almost nothing on a page that isn't a growing list.
        """
        from config import settings

        cap = settings.crawl_max_load_more
        stagnant = 0
        clicks = 0
        for _ in range(cap):
            try:
                before = await page.evaluate("document.body.scrollHeight")
            except Exception:
                break

            clicked = False
            for pat in _LOAD_MORE_PATTERNS:
                loc = page.locator(f'button:has-text("{pat}"), a:has-text("{pat}")').first
                try:
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=2_500)
                        clicked = True
                        clicks += 1
                        break
                except Exception:
                    continue  # button vanished/detached mid-loop — try next pattern

            if not clicked:
                # No load-more control — try infinite-scroll instead.
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    break

            await page.wait_for_timeout(1_200)  # let new items render

            try:
                after = await page.evaluate("document.body.scrollHeight")
            except Exception:
                break

            if after <= before:
                stagnant += 1
                if stagnant >= 2:
                    break  # nothing new twice running — list is exhausted
            else:
                stagnant = 0

        if clicks:
            logger.info(f"[Scraper] Paginated {url} — {clicks} 'load more' step(s)")


web_scraper = WebScraper()
