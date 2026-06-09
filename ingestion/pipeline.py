"""
Chunked extraction pipeline: chunk → filter → extract per-chunk → merge.

Replaces the old "truncate to 12 000 chars and call Qwen once" approach
used by both web_scraper and rss_parser.

Usage
-----
    from ingestion.pipeline import pipeline

    startups = pipeline.run(content, source_url, source, published_date)
    # pipeline.run() returns a merged list of startup dicts.
    # The caller is responsible for persisting them via upsert_startup().

Design
------
* Chunker splits the full content into overlapping 6 000-char windows.
* Candidate filter discards navigation bars, legal pages, event listings,
  etc. — typically dropping ~70 % of chunks before they reach Qwen.
* Each passing chunk is sent to Qwen independently.  Because Qwen's context
  is 8 192 tokens and each chunk is ~1 500 tokens, the prompt + schema fit
  comfortably with room for the response.
* Results from all chunks are merged by normalized company name: the record
  with the most non-null fields wins, and missing fields are back-filled
  from duplicate records found in later chunks.
"""
import logging
from typing import List, Optional

from ingestion.chunker import split as split_chunks
from ingestion.candidate_filter import is_relevant

logger = logging.getLogger(__name__)


class ExtractionPipeline:

    def run(
        self,
        content: str,
        source_url: str,
        source: str,
        published_date: Optional[str] = None,
    ) -> List[dict]:
        """
        Run the full pipeline on *content* and return the merged startup list.

        Steps
        -----
        1. Split content into overlapping chunks.
        2. Filter chunks with the heuristic relevance check.
        3. Call Qwen on each passing chunk.
        4. Merge all extracted startups by normalized name.

        Parameters
        ----------
        content:        Raw aggregated text (crawl output or article body).
        source_url:     URL of the originating page (for logging / attribution).
        source:         Human-readable source label (e.g. "htgf.de", "rss").
        published_date: ISO 8601 date string if known (propagated to each
                        startup dict so upsert_startup() can set published_at).

        Returns
        -------
        List of startup dicts ready for upsert_startup().  May be empty.
        """
        if not content or not content.strip():
            return []

        chunks = split_chunks(content)
        if not chunks:
            return []

        relevant = [c for c in chunks if is_relevant(c)]
        total    = len(chunks)
        kept     = len(relevant)
        pct_drop = round((1 - kept / total) * 100) if total else 0
        logger.info(
            f"[Pipeline] {source_url}: {total} chunk(s) → "
            f"{kept} relevant ({pct_drop}% filtered)"
        )

        if not relevant:
            logger.info(f"[Pipeline] All chunks filtered — no startups expected for {source_url}")
            return []

        all_startups: List[dict] = []
        for i, chunk in enumerate(relevant, 1):
            extracted = self._extract_from_chunk(chunk, source_url, i, kept)
            # Attach published_date so the caller can pass it to upsert_startup
            if published_date:
                for s in extracted:
                    if not s.get("published_date"):
                        s["published_date"] = published_date
            all_startups.extend(extracted)

        merged = _merge_by_name(all_startups)
        logger.info(
            f"[Pipeline] {source_url}: "
            f"{len(all_startups)} raw extractions → {len(merged)} after merge"
        )
        return merged

    # ── Private ───────────────────────────────────────────────────────────────

    def _extract_from_chunk(
        self,
        chunk: str,
        source_url: str,
        chunk_num: int,
        total_chunks: int,
    ) -> List[dict]:
        """Send one chunk to Qwen and return the parsed startup list."""
        import time
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT

        logger.info(f"[Pipeline] Chunk {chunk_num}/{total_chunks}")
        try:
            prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=chunk)
            logger.info("[Pipeline] Sending to Qwen")
            t0 = time.time()
            response = qwen_client.generate(
                prompt,
                system="Return ONLY a valid JSON array. No explanation, no markdown.",
                temperature=0,
                num_ctx=4096,
            )
            logger.info(f"[Pipeline] Qwen completed in {time.time() - t0:.1f}s")
            startups = qwen_client.parse_json_array(response)
            logger.info(
                f"[Pipeline] Chunk {chunk_num}/{total_chunks}: {len(startups)} startup(s) extracted"
            )
            return startups or []
        except Exception as exc:
            logger.error(
                f"[Pipeline] Chunk {chunk_num}/{total_chunks} failed "
                f"({source_url}): {exc}"
            )
            return []


# ── Module-level helpers ──────────────────────────────────────────────────────

def _merge_by_name(startups: List[dict]) -> List[dict]:
    """
    Deduplicate startups extracted from multiple chunks by normalized name.

    When the same startup name appears in two different chunks (overlap
    region or repetition), the first record is kept as the base and any
    non-null fields from later records are used to back-fill missing data.
    Tag lists are merged (union, no duplicates).
    """
    from processing.deduplicator import normalize_company_name

    seen: dict = {}   # normalized_name → startup dict

    for startup in startups:
        name = (startup.get("name") or "").strip()
        if not name:
            continue
        key = normalize_company_name(name)
        if not key:
            continue

        if key not in seen:
            seen[key] = dict(startup)
        else:
            # Back-fill missing fields from this duplicate record
            base = seen[key]
            for field, value in startup.items():
                if value is not None and value != "" and not base.get(field):
                    base[field] = value
            # Merge tags (union)
            if startup.get("tags") and isinstance(base.get("tags"), list):
                base["tags"] = list(set(base["tags"]) | set(startup["tags"]))

    return list(seen.values())


# ── Singleton ─────────────────────────────────────────────────────────────────

pipeline = ExtractionPipeline()
