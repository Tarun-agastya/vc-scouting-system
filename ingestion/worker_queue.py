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
    # Phase R-4 — populated only when settings.adaptive_pipeline_enabled;
    # None/empty otherwise, so the legacy chunking path is untouched.
    strategy: Optional[object] = None          # ingestion.strategy.PageStrategy
    entity_names: List[str] = field(default_factory=list)
    entity_blocks: List[tuple] = field(default_factory=list)  # [(name, text), ...]
    expected_entity_count: int = 0
    parent_entity_name: Optional[str] = None   # R-6 extension point: detail-page attribution


@dataclass
class ChunkItem:
    chunk: str
    source_url: str
    source_type: str
    chunk_num: int
    total_chunks: int
    published_date: Optional[str] = None
    # origin_url = the crawl's start_url (for source-registry name lookup);
    # source_url above stays the actual page this chunk came from. Phase R-4
    # fix: previously only the page URL was threaded through, so
    # storage._resolve_source_name's exact primary_url match failed for
    # every page except the entry URL (measured 31 Jul: 98% of web-sourced
    # source_history entries had source_name=None).
    origin_url: str = ""
    page_url: str = ""
    chunk_kind: Optional[str] = None           # None = legacy; else "prose"|"name_batch"|"card"
    entity_hint: Optional[str] = None          # R-6 extension point


@dataclass
class StorageItem:
    startup_dict: dict
    source: str
    source_url: str
    published_date: Optional[str] = None
    origin_url: str = ""
    # Validation provenance — populated by qwen_worker_task when a
    # ValidationSession is active; zero-cost defaults otherwise.
    page_url:       str   = ""
    chunk_num:      int   = 0
    total_chunks:   int   = 0
    chunk_preview:  str   = ""
    qwen_duration_s: float = 0.0


# ── Metrics ───────────────────────────────────────────────────────────────────

@dataclass
class PageOutcome:
    """
    Per-page expected-vs-actual, the unit the Phase R-5 recall audit works on.

    `expected` is what the page STRUCTURALLY appears to contain (a card-group
    size, a JSON-LD ItemList count, a clean logo-grid name count) — recomputed
    every run, never read from a cached profile. `extracted` holds the distinct
    lowercased names actually produced from this page's chunks.
    """
    url: str
    expected: int = 0
    extracted: set = field(default_factory=set)
    qwen_failures: int = 0
    page_shape: str = ""
    retried: bool = False
    recovered: bool = False

    @property
    def recall(self) -> float:
        """0.0-1.0. Defined as 1.0 when nothing was expected — no expectation,
        no shortfall (prose pages must never trigger a retry)."""
        if self.expected <= 0:
            return 1.0
        return min(1.0, len(self.extracted) / self.expected)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "expected": self.expected,
            "extracted": len(self.extracted),
            "recall": round(self.recall, 3),
            "qwen_failures": self.qwen_failures,
            "page_shape": self.page_shape,
            "retried": self.retried,
            "recovered": self.recovered,
        }


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
    startups_inserted   — new master records inserted (status "new_master")
    updates_staged      — field changes staged for human review (status "staged_update")
    duplicates_staged   — possible-duplicate/anomaly pairs staged (status "staged_duplicate"/"staged_anomaly")
    unchanged           — exact re-extractions with no meaningful change (status "no_op")
    total_processing_time — wall-clock seconds from first URL fetch to last upsert

    Phase R-0 (31 Jul) adaptive-pipeline instrumentation — all default to 0 and
    stay 0 until later phases populate them, so this phase is a pure no-op on
    behaviour. They exist first so every later phase can be measured against an
    R-0 baseline rather than against a guess:
    pages_rendered / pages_static  — headless-render vs plain static fetch
    pagination_clicks / pagination_items_gained — load-more work and its yield
    cards_detected      — repeating card-group items found by the inspector
    entities_expected   — structurally-detected candidate entities (per run,
                          NEVER cached — a cached count cannot notice a site
                          redesign, which is the whole safety property)
    entities_extracted_distinct — distinct names actually extracted
    chunks_bypassed_filter — chunks that skipped the heuristic relevance gate
    name_batch_chunks / card_chunks — chunks by strategy-driven kind
    detail_pages_followed — per-company detail pages crawled
    recall_shortfalls / retries_attempted / retries_recovered — the audit loop
    profile_hits / profile_misses / profile_probes — SiteProfile cache behaviour
    strategy_llm_calls / strategy_llm_failures — the one-call-per-source strategist
    """
    pages_crawled:          int   = 0
    pages_skipped:          int   = 0
    chunks_created:         int   = 0
    chunks_filtered:        int   = 0
    qwen_calls:             int   = 0
    qwen_failures:          int   = 0
    startups_extracted:     int   = 0
    startups_inserted:      int   = 0
    updates_staged:         int   = 0
    duplicates_staged:      int   = 0
    unchanged:              int   = 0
    total_processing_time:  float = 0.0

    # ── Phase R-0: adaptive-pipeline instrumentation ──────────────────────
    pages_rendered:              int = 0
    pages_static:                int = 0
    pagination_clicks:           int = 0
    pagination_items_gained:     int = 0
    cards_detected:              int = 0
    entities_expected:           int = 0
    entities_extracted_distinct: int = 0
    chunks_bypassed_filter:      int = 0
    name_batch_chunks:           int = 0
    card_chunks:                 int = 0
    detail_pages_followed:       int = 0
    recall_shortfalls:           int = 0
    retries_attempted:           int = 0
    retries_recovered:           int = 0
    profile_hits:                int = 0
    profile_misses:              int = 0
    profile_probes:              int = 0
    strategy_llm_calls:          int = 0
    strategy_llm_failures:       int = 0

    # ── Bottleneck-testing instrumentation (3 Aug) ────────────────────────
    # Cumulative wall-clock seconds spent inside each stage. Pure
    # instrumentation, no behaviour change — always on, unrelated to the
    # adaptive_pipeline_enabled flag. Because the 4-stage pipeline overlaps
    # stages (that's the whole point of the queue architecture), these sum
    # to MORE than total_processing_time when stages run concurrently —
    # read them as "how much total work landed in this stage", not as a
    # partition of wall-clock time. qwen_time_s vs total_processing_time is
    # the most direct read of the GPU-mutex ceiling: with
    # max_qwen_workers=1, qwen_time_s approaching total_processing_time
    # means the extraction stage IS the bottleneck.
    fetch_time_s:   float = 0.0   # crawler: static + Playwright fetch/render
    chunk_time_s:   float = 0.0   # chunker: split + candidate-filter
    qwen_time_s:    float = 0.0   # extraction worker: actual Ollama call time
    storage_time_s: float = 0.0   # storage worker: upsert_startup calls

    # url -> PageOutcome. The recall audit (R-5) needs per-page expected-vs-
    # actual, not just run totals: a run can look healthy in aggregate while
    # one page silently yields nothing.
    per_page: dict = field(default_factory=dict, compare=False, repr=False)

    # URLs where pagination ran out of its click budget while the page was
    # STILL growing (never stopped naturally on two stagnant rounds) — the
    # R-5 retry ladder's step-4 signal ("pagination hit the cap") that a
    # higher max_load_more is worth trying, as opposed to a page that simply
    # has no more content to reveal.
    pagination_hit_cap: set = field(default_factory=set, compare=False, repr=False)

    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        compare=False,
        repr=False,
    )

    def inc(self, counter: str, amount: int = 1) -> None:
        """Atomically increment a named counter. Safe to call from any thread."""
        with self._lock:
            setattr(self, counter, getattr(self, counter) + amount)

    def _page(self, url: str) -> "PageOutcome":
        """Get-or-create this page's outcome. Caller must hold _lock."""
        out = self.per_page.get(url)
        if out is None:
            out = PageOutcome(url=url)
            self.per_page[url] = out
        return out

    def record_expectation(self, url: str, expected: int, *, shape: str = "") -> None:
        """
        Record how many entities this page structurally appears to contain.
        Called once per page at fetch time, every run.
        """
        with self._lock:
            out = self._page(url)
            out.expected = expected
            if shape:
                out.page_shape = shape
            self.entities_expected += expected

    def record_extraction(self, url: str, name: str) -> None:
        """Record one distinct extracted company name against its page."""
        if not name:
            return
        key = name.strip().lower()
        if not key:
            return
        with self._lock:
            out = self._page(url)
            if key not in out.extracted:
                out.extracted.add(key)
                self.entities_extracted_distinct += 1

    def record_page_failure(self, url: str) -> None:
        """Record a Qwen failure against a page (drives retry-ladder step 1)."""
        with self._lock:
            self._page(url).qwen_failures += 1

    def shortfall_pages(self, *, ratio: float, min_gap: int) -> list:
        """
        Pages whose extraction fell materially short of what the page
        structurally offered. `min_gap` stops thrash on small pages, where a
        1-of-3 miss is a huge ratio but a trivial absolute loss.
        Pages with expected == 0 (prose, no structural expectation) never
        qualify — absence of an expectation is not evidence of a shortfall.
        """
        with self._lock:
            outcomes = list(self.per_page.values())
        out = [
            o for o in outcomes
            if o.expected > 0
            and o.recall < ratio
            and (o.expected - len(o.extracted)) >= min_gap
        ]
        return sorted(out, key=lambda o: o.expected - len(o.extracted), reverse=True)

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
            f"[Pipeline]  New masters      : {self.startups_inserted}\n"
            f"[Pipeline]  Updates staged   : {self.updates_staged}\n"
            f"[Pipeline]  Duplicates staged: {self.duplicates_staged}\n"
            f"[Pipeline]  Unchanged        : {self.unchanged}\n"
            f"[Pipeline]  Total time       : {self.total_processing_time:.1f}s\n"
            + self._adaptive_report_lines() +
            "[Pipeline] ─────────────────────────────────────────────────────"
        )

    def _adaptive_report_lines(self) -> str:
        """
        Extra report lines for the adaptive pipeline — emitted only once
        something has actually populated them, so a run with the adaptive
        path disabled logs byte-identically to before Phase R-0.
        """
        if not self.entities_expected and not self.pages_rendered and not self.retries_attempted:
            return ""
        recall = (
            round(self.entities_extracted_distinct / self.entities_expected * 100)
            if self.entities_expected else 0
        )
        lines = (
            f"[Pipeline]  Entities expected: {self.entities_expected}\n"
            f"[Pipeline]  Entities extracted: {self.entities_extracted_distinct} ({recall}% recall)\n"
        )
        if self.pages_rendered or self.pages_static:
            lines += f"[Pipeline]  Rendered/static  : {self.pages_rendered}/{self.pages_static}\n"
        if self.pagination_clicks:
            lines += (f"[Pipeline]  Pagination       : {self.pagination_clicks} click(s), "
                      f"+{self.pagination_items_gained} item(s)\n")
        if self.recall_shortfalls or self.retries_attempted:
            lines += (f"[Pipeline]  Recall shortfalls: {self.recall_shortfalls} "
                      f"(retried {self.retries_attempted}, recovered {self.retries_recovered})\n")
        if self.detail_pages_followed:
            lines += f"[Pipeline]  Detail pages     : {self.detail_pages_followed}\n"
        return lines


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
    from ingestion.chunker import split_web_page as split_chunks
    from ingestion.chunker import LOGO_GRID_CHUNK_HEADER
    from ingestion.candidate_filter import is_relevant
    from config import settings
    from ingestion.strategy import PageStrategy

    while True:
        item = await page_queue.get()
        page_queue.task_done()

        if item is _SENTINEL:
            # Forward sentinel downstream and exit
            await chunk_queue.put(_SENTINEL)
            return

        origin_url = item.source_url or item.url
        strategy = item.strategy
        adaptive = bool(
            settings.adaptive_pipeline_enabled and strategy is not None
            and strategy is not PageStrategy.DEFAULT
        )

        _t0 = time.time()
        if adaptive:
            chunks, chunk_kind = _adaptive_chunks(item, strategy)
        else:
            # Legacy path — byte-identical to pre-R-4 behaviour regardless
            # of what a strategy object (if any) claims.
            chunks, chunk_kind = split_chunks(item.text), None

        total = len(chunks)
        if chunk_kind in ("name_batch", "card"):
            # Structurally-curated chunks (a portfolio name batch, one card's
            # own text) never face the heuristic filter built to judge
            # arbitrary prose — the scraper already curated them.
            relevant = chunks
        else:
            relevant = [
                c for c in chunks
                if c.startswith(LOGO_GRID_CHUNK_HEADER) or is_relevant(c)
            ]
        kept = len(relevant)
        filtered = total - kept
        metrics.inc("chunk_time_s", time.time() - _t0)

        metrics.inc("chunks_created", total)
        metrics.inc("chunks_filtered", filtered)
        if chunk_kind in ("name_batch", "card"):
            metrics.inc("chunks_bypassed_filter", kept)
            metrics.inc("name_batch_chunks" if chunk_kind == "name_batch" else "card_chunks", kept)

        pct = round(filtered / total * 100) if total else 0
        logger.info(
            f"[Chunker] {item.url}: {total} chunk(s) → {kept} relevant ({pct}% filtered)"
        )

        for i, chunk in enumerate(relevant, 1):
            await chunk_queue.put(ChunkItem(
                chunk=chunk,
                source_url=item.url,
                origin_url=origin_url,
                source_type=item.source_type,
                chunk_num=i,
                total_chunks=kept,
                published_date=item.published_date,
                chunk_kind=chunk_kind,
                entity_hint=item.parent_entity_name,
                page_url=item.url,
            ))


def _adaptive_chunks(item: "PageItem", strategy) -> tuple:
    """
    Phase R-4: dispatch on strategy.chunking using the structured data the
    scraper already produced (entity_names / entity_blocks) instead of
    embedding a marker string in page text and re-parsing it back out.
    Falls back to ordinary prose chunking of whatever text this page
    produced when the structured data the strategy expects isn't there
    (e.g. a cached profile predicted a shape the live page no longer
    confirms) — never silently drops the page.
    """
    from ingestion.chunker import split_name_batches, split_cards
    from ingestion.chunker import split as split_prose

    if strategy.chunking == "name_batch" and item.entity_names:
        n = strategy.names_per_chunk or _default_names_per_chunk()
        return split_name_batches(item.entity_names, n), "name_batch"
    if strategy.chunking == "per_card" and item.entity_blocks:
        return split_cards(item.entity_blocks), "card"
    return split_prose(item.text), "prose"


def _default_names_per_chunk() -> int:
    try:
        from config.tuning_loader import get_chunking_config
        return int(get_chunking_config().get("names_per_chunk", 6))
    except Exception:
        return 6


# ── Stage 3: Qwen Workers ────────────────────────────────────────────────────

def _qwen_extract_sync(
    item: ChunkItem, metrics: PipelineMetrics
) -> tuple:  # (List[dict], float elapsed_s)
    """
    Synchronous extraction — called via run_in_executor from qwen_worker_task.

    Delegates to qwen_client.extract_startups() which uses the small fast model
    (qwen2.5:7b-instruct) with Ollama structured output for guaranteed-valid JSON
    and a one-retry-on-failure policy with a 45s per-attempt timeout.

    Returns a 2-tuple (startups, elapsed_s).
      startups  — possibly empty list of startup dicts
      elapsed_s — wall-clock seconds spent in the extract call (0.0 on failure)
    Never raises: all exceptions are caught, logged, and counted as qwen_failures.
    """
    from reasoning.qwen_client import qwen_client

    metrics.inc("qwen_calls")
    logger.info(
        f"[Extract Worker] Chunk {item.chunk_num}/{item.total_chunks} — {item.source_url}"
    )
    t0 = time.time()

    try:
        startups = qwen_client.extract_startups(item.chunk, chunk_kind=item.chunk_kind)
        elapsed = time.time() - t0
        logger.info(
            f"[Extract Worker] Chunk {item.chunk_num}/{item.total_chunks}: "
            f"{len(startups)} startup(s) in {elapsed:.1f}s"
        )

        # Propagate published_date to each startup dict if not already set
        if item.published_date:
            for s in startups:
                if not s.get("published_date"):
                    s["published_date"] = item.published_date

        metrics.inc("startups_extracted", len(startups))
        metrics.inc("qwen_time_s", elapsed)
        for s in startups:
            metrics.record_extraction(item.page_url or item.source_url, s.get("name") or "")
        return startups, elapsed

    except Exception as exc:
        elapsed = time.time() - t0
        metrics.inc("qwen_failures")
        metrics.inc("qwen_time_s", elapsed)
        metrics.record_page_failure(item.page_url or item.source_url)
        logger.error(
            f"[Extract Worker] Chunk {item.chunk_num}/{item.total_chunks} failed "
            f"after {elapsed:.1f}s ({item.source_url}): {exc}"
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
                origin_url=item.origin_url or item.source_url,
                published_date=item.published_date or startup.get("published_date"),
                page_url=item.page_url or item.source_url,
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

        t0 = time.time()
        record_id, status = upsert_startup(
            item.startup_dict,
            item.source,
            item.source_url,
            item.published_date,
            origin_url=item.origin_url or None,
        )
        metrics.inc("storage_time_s", time.time() - t0)

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
                stored=bool(record_id),
                record_id=record_id,
            )

        _STATUS_COUNTER = {
            "new_master":       "startups_inserted",
            "staged_update":    "updates_staged",
            "staged_duplicate": "duplicates_staged",
            "staged_anomaly":   "duplicates_staged",
            "no_op":            "unchanged",
        }
        counter = _STATUS_COUNTER.get(status)
        if counter:
            metrics.inc(counter)
