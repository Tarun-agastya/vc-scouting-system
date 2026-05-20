import feedparser
import trafilatura
import uuid
import logging
from typing import List, Optional
from ingestion.sources import RSS_FEEDS

logger = logging.getLogger(__name__)


class RSSParser:
    """
    Ingests startup-focused RSS feeds.
    Fetches articles → extracts text → sends to Qwen for entity extraction
    → stores in Qdrant.
    """

    def __init__(self):
        self.feeds = RSS_FEEDS

    def ingest_feeds(
        self,
        feed_urls: Optional[List[str]] = None,
        max_entries: int = 50
    ) -> List[dict]:
        """Main entry point: ingest one or all RSS feeds."""
        urls = feed_urls or [f["url"] for f in self.feeds]
        all_startups: List[dict] = []

        for url in urls:
            logger.info(f"[RSS] Processing: {url}")
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:max_entries]:
                    startups = self._process_entry(entry, url)
                    all_startups.extend(startups)
            except Exception as exc:
                logger.error(f"[RSS] Failed for {url}: {exc}")

        logger.info(f"[RSS] Total startups extracted: {len(all_startups)}")
        return all_startups

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _process_entry(self, entry, source_url: str) -> List[dict]:
        """Build full text for one feed entry and extract startups."""
        content_parts = []

        if hasattr(entry, "title"):
            content_parts.append(entry.title)
        if hasattr(entry, "summary"):
            content_parts.append(entry.summary)

        # Try to fetch the full article text
        link = getattr(entry, "link", "")
        if link:
            try:
                downloaded = trafilatura.fetch_url(link)
                if downloaded:
                    article_text = trafilatura.extract(downloaded)
                    if article_text:
                        content_parts.append(article_text[:4000])
            except Exception:
                pass  # Silent fail — summary is enough

        full_text = "\n".join(content_parts).strip()
        if len(full_text) < 80:
            return []

        return self._extract_startups(full_text, source_url, link)

    def _extract_startups(self, text: str, source: str, source_url: str) -> List[dict]:
        """
        Call Qwen to extract structured startup data from raw text.
        Import is deferred so this module can be imported before Ollama is ready.
        """
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT

        try:
            prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=text[:3500])
            response = qwen_client.generate(
                prompt,
                system="Return ONLY a valid JSON array. No explanation, no markdown.",
                temperature=0.0,
            )

            startups: List[dict] = qwen_client.parse_json_array(response)
            if not startups:
                return []
            stored = 0
            for startup in startups:
                if startup.get("name") and len(startup["name"]) > 1:
                    self._store_startup(startup, source, source_url)
                    stored += 1

            if stored:
                logger.info(f"[RSS] Stored {stored} startups from {source_url}")
            return startups

        except Exception as exc:
            logger.debug(f"[RSS] Extraction failed: {exc}")
            return []

    def _store_startup(self, startup: dict, source: str, source_url: str):
        """Embed and store one startup in Qdrant."""
        from embeddings.embedder import embedder
        from vector_db.qdrant_store import qdrant_store

        try:
            startup_id = str(uuid.uuid4())
            embed_text = embedder.build_startup_text(startup)
            vector = embedder.embed(embed_text)

            payload = {
                **startup,
                "source": source,
                "source_url": source_url,
                "id": startup_id,
            }
            qdrant_store.upsert_startup(startup_id, vector, payload)
        except Exception as exc:
            logger.error(f"[RSS] Store failed for {startup.get('name')}: {exc}")


rss_parser = RSSParser()
