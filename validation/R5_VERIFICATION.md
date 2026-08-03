# Phase R-5 verification — 3 Aug 2026

Recall audit + bounded auto-retry: `scrape_source` becomes two phases —
Phase A (today's `asyncio.gather` crawl+extract, unchanged) followed by
Phase B, which re-runs the worst recall-shortfall pages from Phase A
through the same chunker/worker/storage stages, one deterministic
retry-ladder step at a time.

All live tests below construct a deliberate shortfall (per the plan's own
verification language — "artificially set... on a paginated source") against
the **real** `zollhof.de` portfolio profile, since no currently-registered
source naturally shortfalls. `upsert_startup` is stubbed (dry-run, no
Postgres/Qdrant writes); `SiteProfile` writes are real but the profile's
original state is saved and restored after every test — confirmed via
`/sources/profiles` after the full test sequence (see below).

## Code changes

- **`config/__init__.py`**: `recall_shortfall_ratio` (0.6), `recall_shortfall_min_gap` (5), `recall_retry_max_pages` (3), `recall_retry_max_calls` (30).
- **`ingestion/worker_queue.py`**: `PipelineMetrics.pagination_hit_cap: set` — the R-5 ladder's step-4 signal.
- **`ingestion/web_scraper.py`**: `_exhaust_pagination` now uses a `for...else` on its click loop — `else` only runs when the loop completed all `cap` iterations without an early `break`, meaning the page was still growing when the budget ran out (as opposed to naturally exhausting). New `_recall_audit_and_retry` method (Phase B), called from `scrape_source` after Phase A when `adaptive_pipeline_enabled`.
- **`processing/site_profile_store.py`**: `next_retry_step(profile, page_outcome, metrics)` — the 7-step ladder, position-tracked so a page never repeats a step already proven not to help. `apply_retry_result(profile_id, ladder_idx, new_strategy, recovered)` — persists the winning strategy on success (`strategy_source="learned"`) or only advances the ladder pointer on failure.
- **New tests**: `tests/test_recall_retry.py` (17 tests) — `PipelineMetrics.shortfall_pages` (ratio/gap-floor/no-expectation-never-qualifies/sort-order), all 7 ladder steps individually, the position-skip guarantee, ladder exhaustion, the qwen-failures-checked-first ordering guarantee, and `apply_retry_result`'s DB round-trip (both outcomes).

## Two bugs found and fixed via live testing, before this phase shipped

### 1. `expected_entity_count` was silently stale on every "hit" page (violates the R-0 safety property)

R-0's `PipelineMetrics` docstring states the safety property this whole
recall-audit design depends on: *"entities_expected — structurally-detected
candidate entities (per run, NEVER cached — a cached count cannot notice a
site redesign)."* Auditing R-4's actual code while building R-5 found this
wasn't true: `_crawler_task`'s "hit" branch (a page whose `SiteProfile`
already exists) set `strategy = strategy_from_profile(known_profile)`,
which reads `expected_entity_count` straight from the **stored** column —
never recomputed from the page's current content. Only a brand-new
("miss") pattern got a fresh count, via `store_deterministic`. Since a
profile persists indefinitely once created, this meant almost every real
page after the first run was comparing extraction against a stale,
potentially months-old expectation — exactly the "can't notice a
redesign" failure R-0 was written to prevent.

**Fix**: `_crawler_task` now recomputes `expected_entity_count` fresh via
`site_inspector.probe_html` on every hit page whose `page_shape` is
entity-bearing, using the strategy's `with_()` to update only that one
field — the cached `text_extraction`/`chunking`/`needs_render`/etc. (how
to process the page) are still reused from the profile as designed; only
what-to-expect is never trusted from cache.

### 2. A single page's own retry could overshoot the whole batch's call ceiling

Live-tested the "recover" scenario against zollhof's real 117-item
portfolio grid: forcing `qwen_failures=1` correctly triggered ladder step 1
(halve `names_per_chunk`, default 6 → 3), but a 117-item grid at 3
names/chunk needs `ceil(117/3) = 39` calls — already more than the entire
batch's `recall_retry_max_calls=30` ceiling, on ONE page alone. The
per-candidate budget check (`if calls_budget <= 0: break`, evaluated
*before* starting each candidate) correctly protects any *subsequent*
candidate, but has no way to stop a retry already in flight — so the very
first page tested silently spent 39 calls against a nominal 30-call cap.

**Fix**: added a pre-flight cost estimate for the two chunk kinds where the
exact call count is knowable before running anything — `ceil(names /
batch_size)` for `name_batch`, `len(entity_blocks)` for `per_card` — and
skip the retry (recording it as a plain shortfall, not spending any Qwen
calls) when the estimate exceeds the remaining budget. Prose/full_text
retries aren't pre-estimated (bounded by content length in practice, lower
risk, and no cheap way to know the count without doing the chunking work
that would itself count against the estimate).

## Live test results

All three tests used the real `zollhof.de` portfolio profile (`page_shape=logo_grid`, `text_extraction=alt_harvest`, `needs_render=True`, `paginate=False`, `names_per_chunk=None`), reset to `retry_ladder_position=0` before each run and restored to its exact original state after.

### Recovery, budget-respecting skip (default `recall_retry_max_calls=30`)

Forced `qwen_failures=1` on a shortfall outcome (`expected=117`, 10 fake
pre-existing names). Ladder step 1 applies (halve batch size 6→3) →
pre-flight estimate `ceil(117/3)=39 > 30` remaining budget → **skipped,
zero Qwen calls spent**, recorded as a plain shortfall
(`consecutive_shortfalls=1`, `retry_ladder_position` correctly left
untouched — the same step will be re-evaluated, not silently marked
"tried", the next time this page is audited). Confirms the pre-flight fix
above actually prevents the overshoot rather than just documenting it.

### Recovery, real extraction (`recall_retry_max_calls=50`)

Same setup, sufficient budget this time: 39 real Qwen calls ran, 114
distinct real company names extracted (of the page's real ~117 — e.g.
"Nouma Autonomy", "Concipio Health", "Lemvos"), `retries_recovered=1`,
`retry_ladder_position` advanced to `1`, `consecutive_shortfalls` reset to
`0`. `apply_retry_result` correctly persisted the winning
`names_per_chunk=3` with `strategy_source="learned"` before the test
restored the original state.

### Unreachable target — flags rather than loops (`expected=999999`, budget 50)

| Attempt | Ladder step | Result | `retry_ladder_position` | `consecutive_shortfalls` | `status` |
|---|---|---|---|---|---|
| 1 | 1 (halve batch size) | 39 real calls, ~114 names found, still far short of 999999 → not recovered | 1 | 1 | `active` |
| 2 | 3 (`needs_render` already true → step 2 skipped; enable `paginate`) | Real re-extraction, still not recovered | 3 | 2 | **`flagged`**, `flag_reason="recall shortfall after retry: 125/999999 (2 consecutive)"` |

Confirms: each call to `_recall_audit_and_retry` makes **exactly one**
retry attempt per shortfall page — there is no loop within a run — and the
cross-run `consecutive_shortfalls` mechanism (already built in R-2,
reused unchanged) correctly escalates to `flagged` after two genuine
failures, which also satisfies `needs_reprobe()`'s
`consecutive_shortfalls >= 2` check, so the next real run gets a fresh
structural probe instead of repeating a ladder that's already proven
exhausted for this target. On failure, `apply_retry_result` correctly left
`text_extraction`/`names_per_chunk`/etc. untouched both times (only the
ladder pointer moved) — confirmed the persisted strategy fields never
drifted from their original values despite two real re-extractions
happening against the live page.

## Post-test integrity check

`GET /health` showed `startups_in_db: 1412` unchanged before and after the
full test sequence (all three tests write through the stubbed
`upsert_startup`, never touching real Postgres/Qdrant). `GET
/sources/profiles` after all tests confirmed the real zollhof portfolio
profile is back to its exact pre-test state (`text_extraction=alt_harvest`,
`needs_render=True`, `paginate=False`, `retry_ladder_position=0`,
`consecutive_shortfalls=0`, `status=active`).

## Test suite

87/87 pytest green (70 pre-R-5 + 17 new in `tests/test_recall_retry.py`).

## Scope not covered by this verification

- A genuinely organic (not deliberately constructed) shortfall on a live
  scheduled sweep — none of the currently-registered, already-profiled
  sources naturally shortfall today; this is expected to surface findings
  over time as new sources are added and profiled cold (R-7's territory).
- Ladder steps 2, 5, 6, 7 (render escalation from a non-rendering page,
  and the extraction-mode fallbacks) were exercised by unit tests but not
  independently live-verified — only steps 1 and 3 fired naturally given
  zollhof's already-`needs_render=True` starting state. The ladder's
  position-tracking and transform logic are identical code paths for every
  step, and steps 1/3's live success is strong evidence the mechanism
  itself works; the untested steps' correctness rests on the unit tests'
  coverage of their specific `PageStrategy.with_()` transforms.
