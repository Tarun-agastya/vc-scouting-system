# Bottleneck testing methods — 3 Aug 2026

Three reusable benchmark tools, plus always-on stage-timing instrumentation
on `PipelineMetrics`, to answer "where does time actually go" with measured
numbers instead of the guesses Phase T's five throughput hypotheses were
originally based on. None write to Postgres/Qdrant (`bench_pipeline.py`
stubs `upsert_startup`; the other two don't reach storage at all).

## Instrumentation (always on, no behaviour change)

`PipelineMetrics` gained four cumulative counters: `fetch_time_s`,
`chunk_time_s`, `qwen_time_s`, `storage_time_s`, wired into `_crawler_task`
(fetch/render), `chunker_task` (split+filter), `_qwen_extract_sync`
(the Ollama call itself), and `storage_worker_task` (`upsert_startup`).
Surfaced in `/ingestion/status` via `scout_controller._METRIC_FIELDS`.
Because the 4-stage pipeline overlaps stages by design, these are
cumulative *work* per stage, not a partition of wall-clock time — they can
sum to more than `total_processing_time`. Read `qwen_time_s` vs
`total_processing_time` as the most direct signal of whether extraction
itself is the bottleneck.

## Three tools

1. **`scripts/bench_pipeline.py <url> [--adaptive off|on|both] [--max-pages N]`**
   Real `web_scraper.scrape_source()` run (dry-run stub by default), reports
   the stage-timing breakdown and, with `--adaptive both`, a side-by-side
   recall + speed comparison (the promoted, reusable version of the ad-hoc
   script used to live-verify Phase R-4).
2. **`scripts/bench_throughput.py <url> [--n-calls N] [--total-chunks N]`**
   Fetches + chunks one real page, then calls `qwen_client.extract_startups()`
   directly N times (cycling the real chunks) to measure the achieved
   calls/sec and latency distribution — the GPU-mutex ceiling, independent
   of crawl/network variance. `--total-chunks` projects a full-sweep
   duration from the measured rate.
3. **`scripts/bench_components.py <url> [--repeats N]`**
   One fetch, then isolated timing of every CPU-bound pure function in the
   pipeline on the *same* HTML: `_extract_text` (BS4) vs `extract_content`
   main_prose (trafilatura), `site_inspector.probe_html`, `chunker.split`/
   `split_web_page`, `candidate_filter.is_relevant`.

## Findings from live test runs

**Component cost is not the bottleneck — confirmed, not assumed.**
`bench_components.py` against zollhof.de's portfolio page (75KB HTML, 117
structural cards): every pure-Python step was sub-25ms (`_extract_text`
10ms, trafilatura 21ms, `probe_html`'s full structural card detection
22ms, chunking <1ms, `is_relevant` <1ms). Trafilatura also confirmed a
real, large boilerplate-reduction effect on this page shape (1,801 →
565 chars, 69%) — consistent with what fixed most of `schwaben.digital`'s
institutional-junk problem in Phase R-4's live testing.

**The GPU-mutex ceiling is real and highly variable per call.**
`bench_throughput.py` against `schwaben.digital/presse` (10 calls, cycling
5 real press-release chunks): 0.067 calls/sec achieved (15.0s/call average),
but with a 2.9s–32.4s range — an ~11x spread depending on chunk content
(dense multi-company blurbs run far longer than empty/no-match chunks).
With `max_qwen_workers=1` (A.3), this per-call cost is the pipeline's hard
floor; no amount of concurrency elsewhere changes it — only Phase T's
"fewer, cleaner, cheaper calls" premise does.

**A real run's wall clock is ~all Qwen time once the crawl is small.**
`bench_pipeline.py --adaptive both --max-pages 3` against `schwaben.digital`:
adaptive-off, `qwen_time_s` was 33.1s of a 33.4s wall clock (99%) — fetch
and chunk stages are noise by comparison on a 3-page run. This is the
clearest, most direct confirmation that Phase T's model-residency (T-1) and
call-reduction (T-2/T-3/T-4) hypotheses are the correct place to invest —
not crawl speed, not chunking, not storage.

## Known display bug fixed during testing

`bench_pipeline.py`'s A/B comparison table printed raw unrounded float
deltas (`-31.299999999999997`) from float subtraction — fixed by rounding
before formatting, same rounding convention as the rest of the report.

## Scope not covered

- A true full-22-source-sweep throughput measurement (Phase T's own
  eventual verification target) — these tools make that measurement cheap
  to run on demand, but weren't run at that scale here given the time each
  single-source test already took live (schwaben's 25-page crawl alone ran
  ~14 minutes during Phase R-4 verification).
- GPU/Ollama-internal profiling (token-level timing, `num_ctx` sizing
  effects) — these tools measure wall-clock only, treating Ollama as a
  black box, consistent with how the rest of this codebase treats it.
