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
    ):
        """Entry point: deep-crawl `url`, extract startups, store in Qdrant."""
        aggregated = await self._deep_crawl(url, max_depth=max_depth, max_pages=max_pages)
        if aggregated:
            await self._extract_and_store(aggregated, url, source_type)
        else:
            logger.warning(f"[Scraper] No content retrieved from {url}")

    # ── BFS Deep Crawler ──────────────────────────────────────────────────────

    async def _deep_crawl(
        self,
        start_url: str,
        max_depth: int = 2,
        max_pages: int = 10,
    ) -> str:
        """
        Asynchronous BFS crawler.

        Starts at `start_url` (depth 0), follows <a href> links at depth 1,
        and their children at depth 2. Only links that share the exact same
        base domain as `start_url` are followed. Stops when `max_pages` unique
        pages have been scraped.

        Returns all page texts concatenated with
            --- Source: <url> ---
        section headers so the LLM can attribute each passage.
        """
        allowed_domain = _base_domain(start_url)
        visited: set = set()
        # Queue items: (url, depth)
        queue: deque = deque([(start_url, 0)])
        page_blocks: list = []

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
                    continue

                text = _extract_text(html)
                if text:
                    page_blocks.append(f"--- Source: {current_url} ---\n{text}")
                    logger.debug(
                        f"[Scraper] [{depth}] Crawled {current_url} "
                        f"({len(text)} chars)"
                    )

                # Only enqueue children if we haven't reached max depth
                if depth < max_depth:
                    for link in _extract_links(html, current_url):
                        if (
                            link not in visited
                            and _base_domain(link) == allowed_domain
                        ):
                            queue.append((link, depth + 1))

        logger.info(
            f"[Scraper] Deep crawl complete — {len(page_blocks)} pages "
            f"scraped from {allowed_domain}"
        )
        return "\n\n".join(page_blocks)

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

    # ── Extraction & Storage ──────────────────────────────────────────────────

    async def _extract_and_store(
        self, content: str, source_url: str, source_type: str
    ):
        """
        Send aggregated multi-page content to Qwen for startup extraction,
        then upsert each result via the central storage layer (PostgreSQL first,
        then Qdrant).

        Content is trimmed to 12 000 chars so the combined context stays within
        Qwen's effective context window for structured extraction.
        """
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT
        from processing.storage import upsert_startup

        context = content[:12_000]

        try:
            prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=context)
            response = qwen_client.generate(
                prompt,
                system="Return ONLY valid JSON array. No explanation.",
                temperature=0.0,
            )

            startups = qwen_client.parse_json_array(response)
            if not startups:
                return
            stored = 0

            for startup in startups:
                name = startup.get("name", "").strip()
                if not name or len(name) < 2:
                    continue
                result_id = upsert_startup(startup, source_url, source_url)
                if result_id:
                    stored += 1

            logger.info(f"[Scraper] Stored {stored} startups from {source_url}")

        except Exception as exc:
            logger.error(f"[Scraper] Extraction/store failed for {source_url}: {exc}")


web_scraper = WebScraper()
