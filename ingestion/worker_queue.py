"""
Worker queue architecture for pipelined startup extraction.

Stages
------
  Stage 1  Crawler Task   →  page_queue   →
  Stage 2  Chunker Task   →  chunk_queue  →
  Stage 3  Qwen Worker(s) →  storage_queue →
  Stage 4  Storage Worker →  PostgreSQL + Qdrant

Back-pressure
-------------
  page_queue    (maxsize = PAGE_QUEUE_SIZE):  crawler blocks when chunker falls behind.
  chunk_queue   (maxsize = CHUNK_QUEUE_SIZE): chunker blocks when Qwen workers fall behind.
  storage_queue (maxsize = STORAGE_QUEUE_SIZE): Qwen workers block when storage falls behind.

Shutdown protocol (sentinel propagation)
-----------------------------------------
  Crawler           → puts None into page_queue when BFS is complete.
  Chunker           → receives None → puts None into chunk_queue → exits.
  Qwen Worker i     → receives None → re-puts None into chunk_queue (for siblings)
                                    → puts None into storage_queue (to signal storage)
                                    → exits.
  Storage Worker    → counts num_qwen_workers None sentinels → exits.

Extension points
----------------
  PageItem.priority : reserved int field for future priority-queue crawling.
  scrape_source()   : url_priority_map kwarg stub ready for future implementation.
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Sentinel value: None in any queue signals "no more items from this upstream stage"
_SENTINEL = None


# ── Data Transfer Objects ─────────────────────────────────────────────────────

@dataclass
class PageItem:
    url: str
    text: str
    source_type: str
    source_url: str           # start_url of the crawl job (for attribution)
    published_date: Optional[str] = None
    priority: int = 0         # extension point: future priority crawling


@dataclass
class ChunkItem:
    chunk: str
    source_url: str
    source_type: str
    chunk_num: int
    total_chunks: int
    published_date: Optional[str] = None


@dataclass
class StorageItem:
    startup_dict: dict
    source: str
    source_url: str
    published_date: Optional[str] = None
    # Validation provenance — populated by qwen_worker_task when a
    # ValidationSession is active; zero-cost defaults otherwise.
    page_url:       str   = ""
    chunk_num:      int   = 0
    total_chunks:   int   = 0
    chunk_preview:  str   = ""
    qwen_duration_s: float = 0.0


# ── Metrics ───────────────────────────────────────────────────────────────────

@dataclass
class PipelineMetrics:
    """
    Thread-safe counters for a single ingestion run.

    Qwen workers execute inside asyncio's default thread executor, so all
    mutations go through inc() which holds a threading.Lock.  The lock is
    held only for a single attribute read-increment-write, so contention is
    negligible even with MAX_QWEN_WORKERS > 1.

    Counter semantics
    -----------------
    pages_crawled       — pages fetched and sent downstream (had non-empty text)
    pages_skipped       — pages where fetch returned empty HTML or empty text
    chunks_created      — total chunks produced by the chunker across all pages
    chunks_filtered     — chunks dropped by the heuristic relevance filter
    qwen_calls          — total Qwen generate() calls dispatched
    qwen_failures       — calls that raised an exception (timeout, parse, etc.)
    startups_extracted  — startup dicts returned by successful Qwen calls
    startups_inserted   — upsert_startup() returned a non-None UUID (insert or update)
    duplicates_detected — startup with valid name where upsert_startup() returned None
    total_processing_time — wall-clock seconds from first URL fetch to last upsert
    """
    pages_crawled:          int   = 0
    pages_skipped:          int   = 0
    chunks_created:         int   = 0
    chunks_filtered:        int   = 0
    qwen_calls:             int   = 0
    qwen_failures:          int   = 0
    startups_extracted:     int   = 0
    startups_inserted:      int   = 0
    duplicates_detected:    int   = 0
    total_processing_time:  float = 0.0

    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        compare=False,
        repr=False,
    )

    def inc(self, counter: str, amount: int = 1) -> None:
        """Atomically increment a named counter. Safe to call from any thread."""
        with self._lock:
            setattr(self, counter, getattr(self, counter) + amount)

    def report(self, source_url: str) -> None:
        """Emit a structured INFO log summarising the completed ingestion run."""
        filtered_pct = (
            round(self.chunks_filtered / self.chunks_created * 100)
            if self.chunks_created else 0
        )
        failure_pct = (
            round(self.qwen_failures / self.qwen_calls * 100)
            if self.qwen_calls else 0
        )
        logger.info(
            "\n[Pipeline] ── Ingestion Report ──────────────────────────────\n"
            f"[Pipeline]  Source           : {source_url}\n"
            f"[Pipeline]  Pages crawled    : {self.pages_crawled}\n"
            f"[Pipeline]  Pages skipped    : {self.pages_skipped}\n"
            f"[Pipeline]  Chunks created   : {self.chunks_created}\n"
            f"[Pipeline]  Chunks filtered  : {self.chunks_filtered} ({filtered_pct}%)\n"
            f"[Pipeline]  Qwen calls       : {self.qwen_calls}\n"
            f"[Pipeline]  Qwen failures    : {self.qwen_failures} ({failure_pct}%)\n"
            f"[Pipeline]  Startups found   : {self.startups_extracted}\n"
            f"[Pipeline]  Startups stored  : {self.startups_inserted}\n"
            f"[Pipeline]  Duplicates       : {self.duplicates_detected}\n"
            f"[Pipeline]  Total time       : {self.total_processing_time:.1f}s\n"
            "[Pipeline] ─────────────────────────────────────────────────────"
        )


# ── Stage 2: Chunker Task ─────────────────────────────────────────────────────

async def chunker_task(
    page_queue: asyncio.Queue,
    chunk_queue: asyncio.Queue,
    metrics: PipelineMetrics,
) -> None:
    """
    Pull PageItems, split + heuristic-filter them, push ChunkItems.

    Runs on the asyncio event loop — chunking and filtering are CPU-light
    string operations that do not need a thread executor.
    Exits when it receives the None sentinel from the crawler.
    """
    from ingestion.chunker import split as split_chunks
    from ingestion.candidate_filter import is_relevant

    while True:
        item = await page_queue.get()
        page_queue.task_done()

        if item is _SENTINEL:
            # Forward sentinel downstream and exit
            await chunk_queue.put(_SENTINEL)
            return

        chunks = split_chunks(item.text)
        total = len(chunks)
        relevant = [c for c in chunks if is_relevant(c)]
        kept = len(relevant)
        filtered = total - kept

        metrics.inc("chunks_created", total)
        metrics.inc("chunks_filtered", filtered)

        pct = round(filtered / total * 100) if total else 0
        logger.info(
            f"[Chunker] {item.url}: {total} chunk(s) → {kept} relevant ({pct}% filtered)"
        )

        for i, chunk in enumerate(relevant, 1):
            await chunk_queue.put(ChunkItem(
                chunk=chunk,
                source_url=item.url,
                source_type=item.source_type,
                chunk_num=i,
                total_chunks=kept,
                published_date=item.published_date,
            ))


# ── Stage 3: Qwen Workers ────────────────────────────────────────────────────

def _qwen_extract_sync(
    item: ChunkItem, metrics: PipelineMetrics
) -> tuple:  # (List[dict], float elapsed_s)
    """
    Synchronous Qwen extraction — called via run_in_executor from qwen_worker_task.

    Returns a 2-tuple (startups, elapsed_s).
      startups  — possibly empty list of startup dicts
      elapsed_s — wall-clock seconds spent in Qwen (0.0 on failure)
    Never raises: all exceptions are caught, logged, and counted as qwen_failures.
    """
    from reasoning.qwen_client import qwen_client
    from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT

    metrics.inc("qwen_calls")
    logger.info(
        f"[Qwen Worker] Chunk {item.chunk_num}/{item.total_chunks} — {item.source_url}"
    )
    logger.info("[Qwen Worker] Sending to Qwen")
    t0 = time.time()

    try:
        prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=item.chunk)
        response = qwen_client.generate(
            prompt,
            system="Return ONLY a valid JSON array. No explanation, no markdown.",
            temperature=0,
            num_ctx=4096,
        )
        elapsed = time.time() - t0
        logger.info(f"[Qwen Worker] Qwen completed in {elapsed:.1f}s")

        # ── DEBUG: raw Qwen response before any parsing ───────────────────────
        logger.debug(
            "[Qwen Worker] RAW RESPONSE | chunk=%d/%d | source=%s"
            " | response_len=%d | response=%r",
            item.chunk_num, item.total_chunks, item.source_url,
            len(response), response[:500],
        )

        t_parse = time.time()
        startups = qwen_client.parse_json_array(response) or []
        parse_elapsed = time.time() - t_parse

        # ── DEBUG: separate timing ────────────────────────────────────────
        logger.debug(
            "[Qwen Worker] TIMING | chunk=%d/%d | ollama=%.2fs | json_parse=%.4fs",
            item.chunk_num, item.total_chunks, elapsed, parse_elapsed,
        )

        # ── DEBUG: per-chunk extraction summary ───────────────────────────────
        logger.debug(
            "[Qwen Worker] CHUNK SUMMARY | chunk_id=%d/%d | source=%s "
            "| preview=%r | raw_qwen_response=%r | parsed_startup_count=%d",
            item.chunk_num, item.total_chunks, item.source_url,
            item.chunk[:120], response[:300], len(startups),
        )

        # Propagate published_date to each startup dict if not already set
        if item.published_date:
            for s in startups:
                if not s.get("published_date"):
                    s["published_date"] = item.published_date

        metrics.inc("startups_extracted", len(startups))
        logger.info(
            f"[Qwen Worker] Chunk {item.chunk_num}/{item.total_chunks}: "
            f"{len(startups)} startup(s) extracted"
        )
        return startups, elapsed

    except Exception as exc:
        metrics.inc("qwen_failures")
        logger.error(
            f"[Qwen Worker] Chunk {item.chunk_num}/{item.total_chunks} failed "
            f"({item.source_url}): {exc}"
        )
        return [], 0.0


async def qwen_worker_task(
    chunk_queue: asyncio.Queue,
    storage_queue: asyncio.Queue,
    metrics: PipelineMetrics,
    worker_id: int,
    *,
    validation_session=None,
) -> None:
    """
    Pull ChunkItems, dispatch Qwen extraction to thread executor, push StorageItems.

    Uses run_in_executor so the synchronous ollama call does not block the
    asyncio event loop — the crawler and chunker continue while Qwen runs.

    Sentinel propagation:
      - Re-puts None into chunk_queue so sibling workers also receive the signal.
      - Puts None into storage_queue so the storage worker counts this worker done.

    validation_session : ValidationSession | None
      When provided, empty-extraction chunks are recorded immediately here.
      Non-empty extractions are recorded by storage_worker_task (after we
      know whether they were stored or deduplicated).
    """
    loop = asyncio.get_event_loop()

    while True:
        item = await chunk_queue.get()
        chunk_queue.task_done()

        if item is _SENTINEL:
            await chunk_queue.put(_SENTINEL)    # wake up the next sibling worker
            await storage_queue.put(_SENTINEL)  # signal storage worker: one worker done
            return

        startups, qwen_duration = await loop.run_in_executor(
            None, _qwen_extract_sync, item, metrics
        )

        # ── Validation: record empty extractions immediately ──────────────────
        if not startups and validation_session is not None:
            validation_session.record(
                page_url=item.source_url,
                chunk_num=item.chunk_num,
                total_chunks=item.total_chunks,
                chunk_preview=item.chunk[:200],
                company_name="",
                startup_dict={},
                qwen_duration_s=qwen_duration,
                stored=False,
                record_id=None,
            )

        for startup in startups:
            name = (startup.get("name") or "").strip()
            if not name or len(name) < 2:
                # DEBUG: case A — Qwen returned valid JSON but name field is
                # absent or too short; startup is silently dropped here.
                logger.debug(
                    "[Qwen Worker] STARTUP DROPPED (name missing/too short) |"
                    " chunk=%d/%d | raw_name=%r | startup=%r",
                    item.chunk_num, item.total_chunks,
                    startup.get("name"), startup,
                )
                continue
            await storage_queue.put(StorageItem(
                startup_dict=startup,
                source=item.source_type,
                source_url=item.source_url,
                published_date=item.published_date or startup.get("published_date"),
                page_url=item.source_url,
                chunk_num=item.chunk_num,
                total_chunks=item.total_chunks,
                chunk_preview=item.chunk[:200],
                qwen_duration_s=qwen_duration,
            ))


# ── Stage 4: Storage Worker ───────────────────────────────────────────────────

async def storage_worker_task(
    storage_queue: asyncio.Queue,
    metrics: PipelineMetrics,
    num_qwen_workers: int,
    *,
    validation_session=None,
) -> None:
    """
    Serial storage worker — pulls StorageItems and calls upsert_startup().

    Serial by design: prevents dedup race conditions on PostgreSQL fingerprint
    and fuzzy-match lookups.  Qdrant upserts are also serialised as a result.

    Exits only after receiving num_qwen_workers None sentinels (one per Qwen
    worker), guaranteeing that all upstream work has been drained before the
    coordinator's asyncio.gather() returns.

    validation_session : ValidationSession | None
      When provided, every upsert outcome (stored or deduplicated) is recorded
      with full provenance from the StorageItem.
    """
    from processing.storage import upsert_startup

    sentinels_seen = 0

    while True:
        item = await storage_queue.get()
        storage_queue.task_done()

        if item is _SENTINEL:
            sentinels_seen += 1
            if sentinels_seen >= num_qwen_workers:
                return
            continue

        result_id = upsert_startup(
            item.startup_dict,
            item.source,
            item.source_url,
            item.published_date,
        )

        # ── Validation capture ────────────────────────────────────────────────
        if validation_session is not None:
            validation_session.record(
                page_url=item.page_url or item.source_url,
                chunk_num=item.chunk_num,
                total_chunks=item.total_chunks,
                chunk_preview=item.chunk_preview,
                company_name=(item.startup_dict.get("name") or "").strip(),
                startup_dict=item.startup_dict,
                qwen_duration_s=item.qwen_duration_s,
                stored=bool(result_id),
                record_id=result_id,
            )

        if result_id:
            metrics.inc("startups_inserted")
        else:
            # upsert_startup returns None when name validation fails or on exception.
            # If the name was valid, count it as a detected duplicate/rejection.
            name = (item.startup_dict.get("name") or "").strip()
            if name and len(name) >= 2:
                metrics.inc("duplicates_detected")
