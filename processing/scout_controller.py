"""
ScoutController — the deterministic ingestion executor (Phase 3).

Purpose
-------
This is the *muscle* of the system: it runs ingestion jobs but makes NO
scouting decisions (those live in the agent, Phase 4).  Its single job is to
guarantee the Mac is never oversubscribed, by enforcing three invariants:

1. **GPU mutex (A.3).**  A single ``asyncio.Lock`` serializes every heavy LLM
   job.  All ingestion runs acquire it; the Phase 4 agent's 14B reasoning
   calls will acquire the SAME lock (``scout_controller.gpu_mutex``) so the
   agent and the extraction loop can never run at once.

2. **Sequential sources.**  Multi-source runs (accelerators / universities /
   all) execute one source at a time — each acquires the mutex independently,
   so a targeted agent request can interleave fairly between sources.

3. **Pre-flight health checks.**  Before any run, Ollama and Qdrant are
   probed.  If either is down the run is *skipped and logged* — never crashes
   the server or the scheduler.

It also keeps a bounded in-memory **run history** so callers (and the agent)
can poll ``GET /ingestion/status`` to know what ran, when, and with what result.

Singleton: import ``scout_controller`` — do not instantiate copies.
"""
import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# How many completed runs to retain in memory before evicting the oldest.
_MAX_HISTORY = 50


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Run record ─────────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    """One ingestion run's lifecycle and outcome."""
    run_id:     str
    kind:       str               # "rss" | "newsletter" | "web" | "all"
    source:     str               # human label or URL
    status:     str = "queued"    # queued | running | completed | failed | skipped | cancelled
    started_at: Optional[str] = None
    ended_at:   Optional[str] = None
    error:      Optional[str] = None
    metrics:    dict = field(default_factory=dict)

    # Live progress (UI-3): the in-flight PipelineMetrics for a running web
    # scrape, so /ingestion/status can show ticking counters mid-run instead
    # of only after the run finishes. thread-safe (PipelineMetrics.inc() is
    # lock-guarded); never serialized directly — see to_dict().
    live_metrics: Any = field(default=None, repr=False)

    # Batch progress: sources within the same sweep (run_all / accelerators /
    # universities) share one batch_id and an incrementing index, so the UI
    # can show "source 3 of 19". None for standalone/targeted runs.
    batch_id:    Optional[str] = None
    batch_index: Optional[int] = None
    batch_total: Optional[int] = None

    def to_dict(self) -> dict:
        # While running, prefer the live in-flight counters over the (still
        # empty) final `metrics` dict, which is only populated once work()
        # returns. Once finished, self.metrics holds the final values.
        metrics = self.metrics
        if self.status == "running" and self.live_metrics is not None:
            metrics = _metrics_to_dict(self.live_metrics)
        return {
            "run_id":     self.run_id,
            "kind":       self.kind,
            "source":     self.source,
            "status":     self.status,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
            "error":      self.error,
            "metrics":    metrics,
            "batch_id":    self.batch_id,
            "batch_index": self.batch_index,
            "batch_total": self.batch_total,
        }


def _metrics_to_dict(metrics) -> dict:
    """Flatten a PipelineMetrics object into a plain dict for the run record."""
    if metrics is None:
        return {}
    return {
        "pages_crawled":         getattr(metrics, "pages_crawled", 0),
        "pages_skipped":         getattr(metrics, "pages_skipped", 0),
        "chunks_created":        getattr(metrics, "chunks_created", 0),
        "chunks_filtered":       getattr(metrics, "chunks_filtered", 0),
        "qwen_calls":            getattr(metrics, "qwen_calls", 0),
        "qwen_failures":         getattr(metrics, "qwen_failures", 0),
        "startups_extracted":    getattr(metrics, "startups_extracted", 0),
        "startups_inserted":     getattr(metrics, "startups_inserted", 0),
        "duplicates_detected":   getattr(metrics, "duplicates_detected", 0),
        "total_processing_time": round(getattr(metrics, "total_processing_time", 0.0), 1),
    }


# ── Controller ───────────────────────────────────────────────────────────────

class ScoutController:

    def __init__(self):
        # Lazily created inside the running event loop so the Lock binds to the
        # correct loop (important on Python 3.9).
        self._gpu_lock: Optional[asyncio.Lock] = None
        self._runs: "OrderedDict[str, RunRecord]" = OrderedDict()
        self._current_run_id: Optional[str] = None
        # run_id -> the asyncio.Task actually executing _execute() for it, so
        # stop_run() can cancel a specific (or the current) run without the
        # blunt "restart the whole API process" hammer that was the only
        # option before (see stop_run's docstring).
        self._tasks: dict = {}

    # ── GPU mutex ──────────────────────────────────────────────────────────────

    @property
    def gpu_mutex(self) -> asyncio.Lock:
        """
        The single GPU mutex.  Acquire it around ANY heavy Ollama job.

        The Phase 4 agent must wrap its 14B reasoning calls in:
            async with scout_controller.gpu_mutex:
                ...
        so agent reasoning never collides with the extraction loop.
        """
        if self._gpu_lock is None:
            self._gpu_lock = asyncio.Lock()
        return self._gpu_lock

    # ── Health checks ──────────────────────────────────────────────────────────

    async def _preflight(self) -> Optional[str]:
        """
        Probe Ollama and Qdrant.  Return None if both healthy, else a reason
        string explaining why a run should be skipped.
        """
        import httpx
        from config import settings

        # Ollama
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            if resp.status_code != 200:
                return f"Ollama returned HTTP {resp.status_code}"
        except Exception as exc:
            return f"Ollama unreachable: {exc}"

        # Qdrant (sync client → run off the event loop)
        try:
            from vector_db.qdrant_store import qdrant_store
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, qdrant_store.get_startup_count)
        except Exception as exc:
            return f"Qdrant unhealthy: {exc}"

        return None

    # ── Run bookkeeping ────────────────────────────────────────────────────────

    def _new_run(
        self, kind: str, source: str, *,
        batch_id: Optional[str] = None,
        batch_index: Optional[int] = None,
        batch_total: Optional[int] = None,
    ) -> RunRecord:
        rec = RunRecord(
            run_id=str(uuid4()), kind=kind, source=source,
            batch_id=batch_id, batch_index=batch_index, batch_total=batch_total,
        )
        self._runs[rec.run_id] = rec
        while len(self._runs) > _MAX_HISTORY:
            self._runs.popitem(last=False)
        return rec

    async def _execute(
        self,
        rec: RunRecord,
        work: Callable[[], Awaitable[dict]],
    ) -> RunRecord:
        """
        Run *work* under the GPU mutex with pre-flight + error capture.

        ``work`` is a zero-arg callable returning a coroutine that performs the
        ingestion and returns a metrics dict.  Never raises — all failures are
        recorded on the RunRecord, including a user-requested stop (status
        "cancelled" — see stop_run()).
        """
        # Registered as soon as this coroutine starts running as a Task —
        # asyncio.current_task() returns the Task wrapping THIS coroutine
        # regardless of how the caller created it (create_task, gather, …),
        # so no caller needs to change how it launches a run.
        task = asyncio.current_task()
        if task is not None:
            self._tasks[rec.run_id] = task

        try:
            reason = await self._preflight()
            if reason:
                rec.status = "skipped"
                rec.error = reason
                rec.ended_at = _now()
                logger.warning(f"[Controller] Skipping '{rec.source}' — {reason}")
                return rec

            async with self.gpu_mutex:
                self._current_run_id = rec.run_id
                rec.status = "running"
                rec.started_at = _now()
                logger.info(f"[Controller] ▶ Running {rec.kind} '{rec.source}' ({rec.run_id})")
                try:
                    rec.metrics = await work() or {}
                    rec.status = "completed"
                    logger.info(f"[Controller] ✓ Completed '{rec.source}' ({rec.run_id})")
                except Exception as exc:
                    rec.status = "failed"
                    rec.error = str(exc)
                    logger.error(f"[Controller] ✗ Failed '{rec.source}': {exc}")
                finally:
                    rec.ended_at = _now()
                    self._current_run_id = None
        except asyncio.CancelledError:
            # User-requested stop (stop_run()). Record it as a distinct
            # outcome rather than "failed" — nothing went wrong, someone
            # just asked it to stop. The `async with` above still releases
            # the GPU mutex correctly even when cancelled mid-work (a
            # cancellation raised inside an `async with` body always runs
            # __aexit__ before propagating).
            rec.status = "cancelled"
            rec.error = "Stopped by user"
            rec.ended_at = _now()
            self._current_run_id = None
            logger.warning(f"[Controller] ⏹ Stopped '{rec.source}' ({rec.run_id})")
            # Re-raise: a multi-source sweep (run_all/run_accelerators/…)
            # calls _execute() once per source via plain `await` inside the
            # SAME task — swallowing the cancellation here would only skip
            # the current source and silently continue to the next one.
            # Re-raising propagates the cancellation up through the whole
            # sweep's await chain so "Stop" actually stops everything, not
            # just the source that happened to be running.
            raise
        finally:
            self._tasks.pop(rec.run_id, None)
        return rec

    def stop_run(self, run_id: Optional[str] = None) -> bool:
        """
        Cancel a run — the current one if run_id is omitted. Returns True if
        a live task was found and a cancellation was requested, False if
        there was nothing to stop (already finished, unknown run_id, or
        nothing running).

        Honest limitation: cancellation is best-effort at the asyncio level.
        It stops the crawl/chunk/storage loops promptly (their next queue
        wait or page fetch raises CancelledError and unwinds cleanly), but a
        single Ollama HTTP call already in flight runs synchronously in a
        worker thread (run_in_executor) and Python cannot forcibly interrupt
        a running thread — that one call keeps Ollama busy until it finishes
        or its own timeout fires, even though this run immediately shows
        "cancelled" and the GPU mutex is released for the next queued job.
        """
        target_id = run_id or self._current_run_id
        if not target_id:
            return False
        task = self._tasks.get(target_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # ── Raw workers (no record — used internally) ────────────────────────────────

    async def _work_rss(self, max_entries: int) -> dict:
        from ingestion.rss_parser import rss_parser
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: rss_parser.ingest_feeds(max_entries=max_entries)
        )
        return {}

    async def _work_web(self, url: str, source_type: str, rec: RunRecord,
                        force_render: bool = False) -> dict:
        from ingestion.web_scraper import web_scraper
        from ingestion.worker_queue import PipelineMetrics

        # Create the metrics object here (not inside scrape_source) and attach
        # it to the run record BEFORE the scrape starts, so /ingestion/status
        # can read live counters (pages/chunks/startups) while it's running —
        # not just after it finishes.
        live = PipelineMetrics()
        rec.live_metrics = live
        result = await web_scraper.scrape_source(
            url, source_type, force_render=force_render, metrics=live
        )
        return _metrics_to_dict(result)

    async def _work_newsletters(self, max_messages: int, days: int = 14) -> dict:
        from ingestion.newsletter_ingestor import newsletter_ingestor
        loop = asyncio.get_event_loop()
        stored = await loop.run_in_executor(
            None, lambda: newsletter_ingestor.run_ingestion(max_messages=max_messages, days=days)
        )
        return {"startups_stored": stored}

    async def _work_recheck(self, limit: int) -> dict:
        """
        Phase H-3. recheck_pending() does its own Layer-2 GPU calls without
        acquiring gpu_mutex itself — this call is already inside it (see
        _execute below), and asyncio.Lock isn't reentrant.
        """
        from processing.verifier import recheck_pending
        return await recheck_pending(limit=limit)

    async def _work_web_verify(self, limit: int) -> dict:
        """
        Phase W. web_verify_pending() does its own Layer-2-equivalent GPU
        calls without acquiring gpu_mutex itself — this call is already
        inside it (see _execute below); asyncio.Lock isn't reentrant. The
        search calls inside it are plain network I/O, not mutex-bound.
        """
        from processing.web_verifier import web_verify_pending
        return await web_verify_pending(limit=limit)

    # ── Public run methods (each = one mutex-guarded record) ─────────────────────

    async def run_rss(
        self, max_entries: int = 50, *,
        batch_id: Optional[str] = None, batch_index: Optional[int] = None, batch_total: Optional[int] = None,
    ) -> RunRecord:
        rec = self._new_run("rss", "rss-feeds", batch_id=batch_id, batch_index=batch_index, batch_total=batch_total)
        return await self._execute(rec, lambda: self._work_rss(max_entries))

    async def run_newsletters(
        self, max_messages: int = 50, days: int = 14, *,
        batch_id: Optional[str] = None, batch_index: Optional[int] = None, batch_total: Optional[int] = None,
    ) -> RunRecord:
        """
        days=14 (default) is the routine scheduled top-up window. Pass a
        much larger value (e.g. 3650) for a one-time backfill of older mail
        the rolling 14-day window never reaches on its own — see
        newsletter_ingestor.run_ingestion's docstring for why that gap
        exists and why it's safe to re-run at any window size.
        """
        label = "gmail-newsletters" if days <= 14 else f"gmail-newsletters (backfill, {days}d)"
        rec = self._new_run("newsletter", label,
                            batch_id=batch_id, batch_index=batch_index, batch_total=batch_total)
        return await self._execute(rec, lambda: self._work_newsletters(max_messages, days))

    async def run_recheck(
        self, limit: int = 20, *,
        batch_id: Optional[str] = None, batch_index: Optional[int] = None, batch_total: Optional[int] = None,
    ) -> RunRecord:
        """Phase H-3: verification recheck, serialized under the GPU mutex like any run."""
        rec = self._new_run("recheck", "verification-recheck",
                            batch_id=batch_id, batch_index=batch_index, batch_total=batch_total)
        return await self._execute(rec, lambda: self._work_recheck(limit))

    async def run_web_verify(
        self, limit: int = 15, *,
        batch_id: Optional[str] = None, batch_index: Optional[int] = None, batch_total: Optional[int] = None,
    ) -> RunRecord:
        """Phase W: web-search verification of the no-source_excerpt backlog, under the GPU mutex."""
        rec = self._new_run("web_verify", "web-verification-sweep",
                            batch_id=batch_id, batch_index=batch_index, batch_total=batch_total)
        return await self._execute(rec, lambda: self._work_web_verify(limit))

    async def run_web_source(
        self, url: str, source_type: str = "general", label: Optional[str] = None, *,
        force_render: bool = False,
        batch_id: Optional[str] = None, batch_index: Optional[int] = None, batch_total: Optional[int] = None,
    ) -> RunRecord:
        rec = self._new_run("web", label or url,
                            batch_id=batch_id, batch_index=batch_index, batch_total=batch_total)
        return await self._execute(rec, lambda: self._work_web(url, source_type, rec, force_render))

    @staticmethod
    def _high_priority_split():
        """HIGH-priority sources split into (accelerators/other, university hubs)."""
        from config.source_registry import get_high_priority_sources, SourceType
        sources = get_high_priority_sources()
        accel = [s for s in sources if s.source_type != SourceType.UNIVERSITY_HUB]
        uni = [s for s in sources if s.source_type == SourceType.UNIVERSITY_HUB]
        return accel, uni

    async def run_accelerators(self) -> list:
        """Run every HIGH-priority non-university source sequentially."""
        accel, _ = self._high_priority_split()
        bid, total = str(uuid4()), len(accel)
        results = []
        for i, s in enumerate(accel, start=1):
            results.append(await self.run_web_source(
                s.primary_url, s.source_type.value, label=s.source_name,
                force_render=(getattr(s, "render_mode", "auto") == "always"),
                batch_id=bid, batch_index=i, batch_total=total,
            ))
        return results

    async def run_universities(self) -> list:
        """Run every HIGH-priority university-hub source sequentially."""
        _, uni = self._high_priority_split()
        bid, total = str(uuid4()), len(uni)
        results = []
        for i, s in enumerate(uni, start=1):
            results.append(await self.run_web_source(
                s.primary_url, s.source_type.value, label=s.source_name,
                force_render=(getattr(s, "render_mode", "auto") == "always"),
                batch_id=bid, batch_index=i, batch_total=total,
            ))
        return results

    async def run_all(self) -> None:
        """
        The 'big sweep' — RSS, then accelerators, then universities, then
        newsletters.  Sources are staggered by virtue of running sequentially,
        each acquiring the mutex in turn so heavy jobs never overlap. All
        phases share one batch_id + a single running index/total across the
        whole sweep, so the UI can show "source N of TOTAL" end-to-end.
        """
        accel, uni = self._high_priority_split()
        bid = str(uuid4())
        total = 1 + len(accel) + len(uni) + 1  # rss + accelerators + universities + newsletters
        i = 1

        await self.run_rss(batch_id=bid, batch_index=i, batch_total=total)
        i += 1
        for s in accel:
            await self.run_web_source(
                s.primary_url, s.source_type.value, label=s.source_name,
                force_render=(getattr(s, "render_mode", "auto") == "always"),
                batch_id=bid, batch_index=i, batch_total=total,
            )
            i += 1
        for s in uni:
            await self.run_web_source(
                s.primary_url, s.source_type.value, label=s.source_name,
                force_render=(getattr(s, "render_mode", "auto") == "always"),
                batch_id=bid, batch_index=i, batch_total=total,
            )
            i += 1
        await self.run_newsletters(batch_id=bid, batch_index=i, batch_total=total)

    # ── Targeted command surface (the agent's lever) ─────────────────────────────

    def submit_targeted(
        self,
        *,
        kind: Optional[str] = None,
        source_id: Optional[str] = None,
        url: Optional[str] = None,
        source_type: str = "general",
    ) -> str:
        """
        Resolve a single ingestion target, schedule it under the mutex, and
        return its run_id immediately so the caller can poll ``status(run_id)``.

        Resolution order:
          - kind == "rss"        → all RSS feeds
          - kind == "newsletter" → Gmail newsletters
          - source_id            → registry source (web scrape)
          - url                  → ad-hoc web scrape

        Raises ValueError on an unresolvable request (route → HTTP 422).
        Must be called from within the running event loop.
        """
        if kind == "rss":
            rec = self._new_run("rss", "rss-feeds")
            work: Callable[[], Awaitable[dict]] = lambda: self._work_rss(50)
        elif kind == "newsletter":
            rec = self._new_run("newsletter", "gmail-newsletters")
            work = lambda: self._work_newsletters(50)
        elif source_id:
            src = self._find_registry_source(source_id)  # raises ValueError if unknown
            target_url, target_type = src.primary_url, src.source_type.value
            render = getattr(src, "render_mode", "auto") == "always"
            rec = self._new_run("web", src.source_name)
            work = lambda: self._work_web(target_url, target_type, rec, render)
        elif url:
            rec = self._new_run("web", url)
            work = lambda: self._work_web(url, source_type, rec)
        else:
            raise ValueError(
                "targeted run requires kind='rss'|'newsletter', a source_id, or a url"
            )

        asyncio.create_task(self._execute(rec, work))
        return rec.run_id

    @staticmethod
    def _find_registry_source(source_id: str):
        from config.source_registry import SOURCE_REGISTRY
        for s in SOURCE_REGISTRY:
            if s.source_id == source_id:
                return s
        raise ValueError(f"unknown source_id '{source_id}'")

    # ── Status ───────────────────────────────────────────────────────────────────

    @property
    def current_run_id(self) -> Optional[str]:
        """
        The run_id currently executing under the GPU mutex, or None if no
        controller-managed run is in flight (e.g. a manual /add-startup call).
        Read by processing/storage.py to stamp provenance on every write.
        """
        return self._current_run_id

    def get_run(self, run_id: str) -> Optional[dict]:
        rec = self._runs.get(run_id)
        return rec.to_dict() if rec else None

    def status(self, run_id: Optional[str] = None) -> dict:
        """
        Snapshot of controller state.  If *run_id* is given, the matching run is
        returned under "run" (for polling a targeted request); otherwise the
        current run, last finished run, and recent history are returned.
        """
        if run_id is not None:
            return {"run": self.get_run(run_id)}

        current = self._runs.get(self._current_run_id) if self._current_run_id else None
        finished = [
            r for r in self._runs.values()
            if r.status in ("completed", "failed", "skipped", "cancelled")
        ]
        last_run = finished[-1] if finished else None
        recent = list(self._runs.values())[-10:][::-1]

        return {
            "gpu_locked":  self.gpu_mutex.locked(),
            "current_run": current.to_dict() if current else None,
            "last_run":    last_run.to_dict() if last_run else None,
            "history":     [r.to_dict() for r in recent],
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

scout_controller = ScoutController()
