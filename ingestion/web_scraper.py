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


def _extract_text(html: str) -> str:
    """Strip boilerplate tags and return clean plain text from raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


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
        max_depth: int = 2,
        max_pages: int = 10,
        *,
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
        t0 = time.time()
        num_workers = settings.max_qwen_workers

        page_queue    = asyncio.Queue(maxsize=settings.page_queue_size)
        chunk_queue   = asyncio.Queue(maxsize=settings.chunk_queue_size)
        storage_queue = asyncio.Queue(maxsize=settings.storage_queue_size)

        await asyncio.gather(
            self._crawler_task(
                url, source_type, max_depth, max_pages, page_queue, metrics
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
        # Queue items: (url, depth)
        queue: deque = deque([(start_url, 0)])

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=self._http_timeout,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            while queue and len(visited) < max_pages:
                current_url, depth = queue.popleft()

                if current_url in visited:
                    continue
                visited.add(current_url)

                html = await self._fetch_page(client, current_url)
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
                        if link in visited or _base_domain(link) != allowed_domain:
                            continue
                        if _is_irrelevant_url(link):
                            logger.debug(f"[Scraper] Skipping irrelevant URL: {link}")
                            continue
                        queue.append((link, depth + 1))

        logger.info(
            f"[Scraper] Crawl complete — {metrics.pages_crawled} pages "
            f"scraped from {allowed_domain}"
        )
        await page_queue.put(None)  # sentinel: signals chunker that crawl is done

    # ── Fetch Strategies ──────────────────────────────────────────────────────

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> str:
        """
        Fetch a single page with httpx.
        Falls back to headless Playwright for JS-rendered sites.
        """
        try:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "text/html" in content_type:
                return resp.text
            logger.debug(
                f"[Scraper] Skipped {url} — "
                f"status={resp.status_code} content-type={content_type}"
            )
        except Exception as exc:
            logger.debug(f"[Scraper] httpx failed for {url}: {exc}")

        # Playwright fallback for JS-gated / Cloudflare-protected pages
        return await self._fetch_playwright(url)

    async def _fetch_playwright(self, url: str) -> str:
        """JavaScript-aware fetch using Playwright (headless Chromium)."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_extra_http_headers({"User-Agent": _HEADERS["User-Agent"]})
                # domcontentloaded is faster than networkidle; avoids hanging on
                # sites with perpetual background analytics/chat widgets.
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                html = await page.content()
                await browser.close()
            return html
        except Exception as exc:
            logger.error(f"[Scraper] Playwright failed {url}: {exc}")
        return ""


web_scraper = WebScraper()
