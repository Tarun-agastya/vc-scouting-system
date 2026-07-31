# Phase R-0 baseline — 31 Jul 2026

The pre-adaptive-pipeline snapshot every later Phase R increment is diffed
against. Recorded before any behaviour change; R-0 itself is instrumentation
only (additive `PipelineMetrics` fields + reporting), verified to leave the
ingestion report byte-identical when the adaptive counters are unused.

## Extraction results to preserve

| Source | Measurement | Baseline | Gate |
|---|---|---|---|
| `zollhof.de/startup-incubation/portfolio` | startups extracted (dry run, no DB writes) | **121** from 120 alt entries / 118 real companies | R-4 flag-on must be **>= 118**, no junk |
| `schwaben.digital` (homepage) | alt entries surviving the junk filter | **10 raw -> 3 clean**, below the 12-entry grid gate | must stay **<= 5 clean, zero junk records** |
| `schwaben.digital` (full crawl, 25 pages) | records written | **0 legitimate** (the 31 Jul run wrote 72, all junk, all deleted) | must not write filename/staff/sponsor records |

## Cost baseline (the hard acceptance criterion)

A full crawl of `schwaben.digital` on 31 Jul, 25 pages:

| Metric | Value |
|---|---|
| pages_crawled | 25 |
| chunks_created | 110 |
| chunks_filtered | 10 |
| **qwen_calls** | **100** |
| qwen_failures | 0 |
| startups_extracted | 223 |
| total_processing_time | 2086.2s (~35 min) |

`zollhof.de/startup-incubation/portfolio`, single page, 6 names/chunk:
21 chunks, 21 kept, **21 qwen_calls**, 0 failures, ~33s per call.

**Gate for R-4 and R-7: total `qwen_calls` across a sweep must not exceed
these figures.** Per Addendum 4's governing rule, the only way to go faster is
fewer, cleaner, cheaper calls — so if strategy-driven chunking does not buy
back the budget that auto-retry (R-5) spends, R-5 does not ship as-is.

## Structural facts confirmed at R-0 (via `scripts/inspect_site.py`)

- `zollhof.de/` — the registered `primary_url` — is the **homepage**: 8 clean
  alts, not 120. The grid is at `/startup-incubation/portfolio`, reached by
  BFS. **A profile keyed only by source would be wrong**; this is why
  `SiteProfile` is keyed `(domain, url_pattern)`.
- `zollhof.de/` static fetch yields 41 text chars vs 1,997 rendered — a
  **48.7x render gain**, an unambiguous `needs_render` signal that today is
  only captured by a hand-written `render_mode: always` in YAML.
- `schwaben.digital` static fetch is already substantial (3,224 text chars),
  so it correctly never escalates to a render.

## Pre-existing bugs recorded here (fixed in later R phases)

1. `chunker_task` sets `ChunkItem.source_url = item.url` (the *page* URL),
   dropping the crawl origin, so `storage._resolve_source_name`'s exact
   `primary_url ==` match fails for every page but the entry URL.
   **Measured: 913 of 936 web-sourced `source_history` entries (98%) have
   `source_name: None`.** Fixed in R-4 by threading `origin_url`.
2. `_metrics_to_dict` emitted a `duplicates_detected` key that
   `PipelineMetrics` never defined, while the dashboard renders tiles for
   `updates_staged`/`duplicates_staged` that were never emitted — so all three
   read 0 forever. **Fixed in R-0.**
