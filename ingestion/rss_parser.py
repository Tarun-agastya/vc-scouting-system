import feedparser
import trafilatura
import logging
from datetime import datetime
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

    # ── Date Helper ───────────────────────────────────────────────────────────

    def _get_published_date(self, entry) -> Optional[str]:
        """Return ISO 8601 publish date string from a feedparser entry, or None."""
        if getattr(entry, "published_parsed", None):
            try:
                return datetime(*entry.published_parsed[:6]).isoformat()
            except Exception:
                pass
        raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
        if raw:
            try:
                import email.utils
                return email.utils.parsedate_to_datetime(raw).isoformat()
            except Exception:
                pass
        return None

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

        published_date = self._get_published_date(entry)
        return self._extract_startups(full_text, source_url, link, published_date)

    def _extract_startups(self, text: str, source: str, source_url: str, published_date: Optional[str] = None) -> List[dict]:
        """
        Run the chunked extraction pipeline on article text.

        Phase 3: replaces the old text[:3500] + single Qwen call.
        Import is deferred so this module can be loaded before Ollama is ready.
        """
        from ingestion.pipeline import pipeline

        try:
            startups = pipeline.run(text, source_url, source, published_date)
            stored = 0
            for startup in startups:
                if startup.get("name") and len(startup["name"]) > 1:
                    self._store_startup(startup, source, source_url, published_date)
                    stored += 1
            if stored:
                logger.info(f"[RSS] Stored {stored} startups from {source_url}")
            return startups
        except Exception as exc:
            logger.debug(f"[RSS] Extraction failed: {exc}")
            return []

    def _store_startup(self, startup: dict, source: str, source_url: str, published_date: Optional[str] = None):
        """Write to PostgreSQL first, then sync to Qdrant via the central storage layer."""
        from processing.storage import upsert_startup
        try:
            record_id, _ = upsert_startup(startup, source, source_url, published_date)
            if not record_id:
                logger.debug(f"[RSS] Skipped (no name): {startup}")
        except Exception as exc:
            logger.error(f"[RSS] Store failed for {startup.get('name')}: {exc}")


rss_parser = RSSParser()
