import uuid
import logging
from typing import Optional
from bs4 import BeautifulSoup
import trafilatura

logger = logging.getLogger(__name__)


class WebScraper:
    """
    Scrapes startup source pages (accelerators, incubators, university hubs).
    Uses trafilatura for static sites; Playwright for JS-heavy pages.
    """

    async def scrape_source(self, url: str, source_type: str = "general"):
        """Async: fetch content and extract startups from one URL."""
        content = await self._fetch_static(url)

        if not content or len(content) < 400:
            logger.info(f"[Scraper] Falling back to Playwright for: {url}")
            content = await self._fetch_playwright(url)

        if content:
            await self._extract_and_store(content, url, source_type)
        else:
            logger.warning(f"[Scraper] Could not retrieve content from {url}")

    # ── Fetch Strategies ──────────────────────────────────────────────────────

    async def _fetch_static(self, url: str) -> str:
        """Lightweight fetch using trafilatura."""
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                return trafilatura.extract(downloaded) or ""
        except Exception as exc:
            logger.debug(f"[Scraper] Static fetch failed {url}: {exc}")
        return ""

    async def _fetch_playwright(self, url: str) -> str:
        """JavaScript-aware fetch using Playwright (headless Chromium)."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                html = await page.content()
                await browser.close()

            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except Exception as exc:
            logger.error(f"[Scraper] Playwright failed {url}: {exc}")
        return ""

    # ── Extraction & Storage ──────────────────────────────────────────────────

    async def _extract_and_store(self, content: str, source_url: str, source_type: str):
        """Call Qwen to extract startups, then store in Qdrant."""
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT
        from embeddings.embedder import embedder
        from vector_db.qdrant_store import qdrant_store

        try:
            prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=content[:4000])
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

                startup_id = str(uuid.uuid4())
                embed_text = embedder.build_startup_text(startup)
                vector = embedder.embed(embed_text)

                payload = {
                    **startup,
                    "id": startup_id,
                    "source": source_url,
                    "source_type": source_type,
                }
                qdrant_store.upsert_startup(startup_id, vector, payload)
                stored += 1

            logger.info(f"[Scraper] Stored {stored} startups from {source_url}")

        except Exception as exc:
            logger.error(f"[Scraper] Extraction/store failed for {source_url}: {exc}")


web_scraper = WebScraper()
