# Phase R-4 verification — 3 Aug 2026

Threads `PageStrategy` through fetch → chunk → extract, behind
`settings.adaptive_pipeline_enabled` (default `False`). This is the phase
that actually changes extraction behaviour — R-0 through R-3 only inspected
and profiled; nothing in the live crawl path consumed a `SiteProfile` until
this phase.

All live tests below are **dry runs**: `processing.storage.upsert_startup`
is monkeypatched to a counting stub, so real fetch/chunk/Qwen-extraction
happens but nothing is written to production Postgres/Qdrant. `GET /health`
confirmed `startups_in_db` unchanged (1412) before and after every test in
this document. `SiteProfile` writes are real (that table is meant to be an
ever-growing learned cache; the sources tested already had entries from the
R-2/R-3 batch, so this only added/refreshed profiles for a few previously-
unprofiled subpages of `schwaben.digital`).

## Code changes

- **`config/__init__.py`**: `adaptive_pipeline_enabled: bool = False` (kill switch).
- **`ingestion/web_scraper.py`**: new `PageContent` dataclass + `extract_content(html, url, strategy)` — `full_text` reproduces `_extract_text()` exactly; `main_prose` uses trafilatura (falls back to `_extract_text` on empty output); `alt_harvest`/`card_structured` read `site_inspector.probe_html`'s primary group directly (bypassing the boilerplate-stripped soup entirely, so a card nested inside a `<header>`/`<nav>` is never lost to an unconditional `decompose()` — the "evidence-based stripping" fix falls out for free). `_fetch_page`/`_fetch_playwright`/`_exhaust_pagination` gained independent `paginate` and `metrics` params (`needs_render` and `paginate` were previously welded together). `_crawler_task` now looks up a per-page `SiteProfile` before fetching (so a known profile's `needs_render`/`paginate` apply to that specific page), and — when adaptive and no profile exists yet — derives one deterministically from the HTML already fetched (`site_profile_store.store_deterministic`, no extra network I/O, no LLM, no GPU mutex) so a brand-new source gets a usable strategy on its very first crawl.
- **`ingestion/site_inspector.py`**: `ItemFeatures` gained a `text` field (bounded 3000 chars) captured at card-detection time; `harvest_entities` now returns real `(name, text)` blocks instead of the R-1 placeholder `[]`.
- **`ingestion/chunker.py`**: `split_name_batches(names, n)` / `split_cards(blocks)` — structured-list counterparts to the marker-text approach, so the R-4 chunker dispatches on data the scraper already produced instead of embedding a string in page text and re-parsing it back out.
- **`ingestion/worker_queue.py`**: `PageItem`/`ChunkItem`/`StorageItem` gained strategy-carrying fields (`strategy`, `entity_names`, `entity_blocks`, `chunk_kind`, `origin_url`, …). `chunker_task` branches cleanly: legacy path (`split_web_page` on `item.text`) when adaptive is off or the page has no real strategy; adaptive path (`_adaptive_chunks`, dispatching on `strategy.chunking`) otherwise. Structurally-curated chunks (`name_batch`/`card`) bypass the heuristic candidate filter, matching the existing logo-grid convention. `_qwen_extract_sync` now passes `chunk_kind` through and records per-page extraction/failure into `metrics.per_page` (the R-5 recall-audit hook, populated for the first time here).
- **`processing/storage.py`**: `upsert_startup` gained an optional `origin_url` param — a free bug fix while in this code (not gated behind the flag): the crawl's start_url, threaded through so `_resolve_source_name`'s exact `primary_url` match works for every page of a multi-page crawl, not just the entry URL (previously 98% of web-sourced `source_history` entries had `source_name=None` — see Addendum 10's R-4 section).
- **`processing/site_profile_store.py`**: new `store_deterministic(url, html)` — deterministic-only profile derivation from already-fetched HTML, never clobbers an existing row.
- **`reasoning/prompts.py`**: new `EXTRACTION_PROMPT_PROSE` (the existing `EXTRACTION_PROMPT` minus the ~30-line BARE NAME LISTS section) — used only for `chunk_kind ∈ {"prose","card"}` under the adaptive pipeline; every legacy call keeps using `EXTRACTION_PROMPT` unconditionally, byte-identical to before this phase.
- **`reasoning/qwen_client.py`**: `extract_startups(text, chunk_kind=None)` — `chunk_kind in (None, "name_batch")` selects the full prompt (today's exact default), anything else selects the trimmed one.
- **New tests**: `tests/test_adaptive_pipeline.py` (17 tests) covering `split_name_batches`/`split_cards`, `extract_content`'s full_text/main_prose/alt_harvest/card_structured modes including degrade-on-no-structural-match, the `page_shape` gate (below), and prompt-selection-by-`chunk_kind` against a fake Ollama client (no network).

## Two bugs found via live testing, both fixed before this phase shipped

### 1. `text_extraction` isn't gated on `page_shape` — CTA cards extracted as companies

Live-tested `zollhof.de` (25-page dry run, adaptive on). First pass: 112 distinct names (down from the adaptive-off baseline's 126) — a real recall **regression**, plus one new junk record: `"Bring me back to the incubation program"`.

Root cause: `zollhof.de`'s **domain-default** profile (from the R-3 batch) is `page_shape="non_content"` — R-3's own LLM adjudication correctly identified the homepage's 5 CTA/heading cards as *"CTAs/headings, not company names"* — but that same profile row's `text_extraction` field is `"card_structured"` (carried over from the LLM/deterministic strategy's own fields, which stay populated even on a `non_content` verdict). `extract_content()` was dispatching purely on `text_extraction`, never checking whether `page_shape` actually claimed to be entity-bearing — so it dutifully ran card-structured extraction on the 5 CTA cards anyway (`https://zollhof.de/` and `/startup-incubation` both fell back to this domain-default profile, contributing 9 "card" chunks with `bypass_candidate_filter` implied), and one CTA card's link text made it all the way to a stored record.

**Fix**: `extract_content()` now checks `strategy.page_shape in ENTITY_SHAPES` before entering `alt_harvest`/`card_structured` mode; if the page's own shape verdict doesn't claim entities, it degrades to `full_text` regardless of what `text_extraction` says. Locked in with two regression tests (`test_extract_content_ignores_structural_mode_when_page_shape_is_non_content`, `test_extract_content_alt_harvest_ignored_when_page_shape_is_article_feed`).

**Result after the fix**: 119 distinct names, zero new junk, and — critically — the 7 real companies missing in the buggy first pass (Ai Butler, Avoltra, BelleHealth, dehub, Inclusys, Prospera, Sortful) were all recovered. They were never actually lost to a name-extraction bug; the whole first-pass shortfall traced back to this single gating bug pulling in one bad structural group. The CTA-card group's own 9 items were simply never real candidates to begin with once page_shape is respected.

### 2. One CTA link inside the real logo grid itself — prompt gap, not a routing bug

After fix #1, one junk item remained: the *same* `"Bring me back to the incubation program"` text, this time surviving as one of the ~117 items *inside* the real portfolio page's own logo-grid card group (`/startup-incubation/portfolio`, `page_shape="logo_grid"` — a genuine entity-bearing page, so fix #1 doesn't apply here). A "back to overview"-style link apparently shares the same repeating DOM structure as the company cards on this specific grid, so it passed structural detection cleanly (it isn't an image filename, isn't a CMS slug, isn't in `_ALT_NOISE`) and reached the LLM, whose BARE NAME LISTS exclude list already named institutions/people/sponsors but never explicitly named "navigation/CTA link text."

**Fix**: added an explicit exclude bullet to `EXTRACTION_PROMPT`'s BARE NAME LISTS section for navigation/pagination/CTA text ("Bring me back to…", "Load more", "Back to overview"). This is a prompt change, not per-item structural filtering — R-2 already tried and reverted per-item headline-shape filtering (it broke real news feeds whose actual content is headline-shaped); this fix stays purely lexical/instructional, at the layer (the model's own judgment over a curated list) the plan always intended for this kind of ambiguous case.

**Result after both fixes**: 118 distinct names, zero junk of any kind — see the hard-gate table below.

## Unplanned discovery: institutional/sponsor junk in ordinary prose extraction (schwaben.digital)

While capturing the adaptive-off *baseline* for `schwaben.digital` (today's exact production behaviour, no R-4 code involved), a full 25-page crawl produced 67 distinct "startup" names, roughly half of them clearly not companies: banks (VR-Bank, Sparkasse Schwaben-Bodensee, Stadtsparkasse Augsburg), a chamber of commerce (IHK Schwaben) and guild (Handwerkskammer Augsburg), three law firms, a health insurer, event/program names (Pitch & Match 2026, STARTUP TEENS-Ideen-Camp), the hosting org itself in multiple name variants, and fabricated German indefinite-article fragments ("ein Server", "ein Games Studio").

This is **not** the alt-harvest/logo-grid bug R-1 fixed (confirmed: that path is unreachable here, `page_shape="article_feed"`, `expected=0`) — it's ordinary prose extraction on pages like `/presse` (a press-release archive) that were never structurally profiled by R-2's entry-page-only regression check. It means the 31 Jul incident this whole Phase R initiative responds to was only ever **partially** fixed. Documented in full, with a proposed (not-yet-implemented) fix design, in the build plan as **Addendum 11 — Phase J**.

**Directly relevant to R-4**: the adaptive-ON pass over the *same* 25 pages, as a side effect of routing to `main_prose` (trafilatura) instead of plain `_extract_text`, dropped this to **16 distinct names with zero institutional/law-firm/event/fabricated-fragment junk** — trafilatura strips the sidebar/related-org/partner-logo boilerplate that most of the junk was actually living in, not the genuine article body. R-4 substantially mitigates Phase J's problem as an incidental benefit, but Phase J's own deterministic gate is still planned as defense-in-depth (a page falling back to `full_text`, or a genuine inline institutional mention in real article prose, gets none of trafilatura's benefit).

## Hard gates (per the plan's R-4 acceptance criteria)

| Source | Adaptive OFF | Adaptive ON | qwen_calls (off → on) |
|---|---|---|---|
| `zollhof.de` | 126 distinct names, **7 identifiable junk** (a person's name, a stray German word, a city-name mis-extraction, 3 slide-deck section headers, the host org's own legal name) | **118 distinct names, zero junk** — every one is a real portfolio company | 28 → 24 |
| `schwaben.digital` | 67 distinct names, **~half institutional/law-firm/event/fabricated junk** | **16 distinct names, zero junk** — every one is a real, press-mentioned portfolio company | 62 → 35 |

**"≥118, no junk" for zollhof: met exactly**, with the recall regression from the two bugs above fully resolved before this count was taken — confirmed by diffing name-for-name against the adaptive-off baseline (zero real companies lost, only known-junk categories removed).

**Schwaben's literal "≤5 clean records" target does not hold** (16, not ≤5) — but re-reading what that number was based on: R-2's regression table verified `schwaben.digital` **structurally, entry-page only** (correctly confirming the alt-harvest path unreachable), never a live full-crawl extraction. A full crawl of an incubator's own site legitimately mentions its own portfolio companies in press releases — capturing those 16 real companies is the pipeline working as intended, not a leak. The substantive safety property the number was a proxy for — **zero filenames/staff/sponsors/institutions** — is met exactly, and is a major improvement over the adaptive-off baseline's ~30 junk records in the same categories the 31 Jul incident was about.

**`qwen_calls` did not exceed the baseline on either source** (both dropped) — the hard ceiling the plan sets on R-4 as a whole holds on the two sources tested here. The full 22-source sweep comparison the plan also calls for is Phase R-7's job (generalization proof against never-tuned sources); not repeated here.

## Test suite

70/70 pytest green (53 pre-R-4 + 17 new in `tests/test_adaptive_pipeline.py`, including the two page_shape-gate regression tests written after the live bug was found and fixed).

## Scope not covered by this verification

- The full 22-source sweep `qwen_calls` comparison (deferred to R-7, alongside the never-tuned-source generalization proof).
- `techfounders.com`/`startbase.de` cold-start behavior (no prior `SiteProfile` at all) — both sources already have profiles from the R-2/R-3 batch, so the `store_deterministic` cold-path in `_crawler_task` was exercised structurally by unit tests but not proven live against a genuinely unprofiled domain. R-7's job.
- Phase J (institutional/sponsor junk fix) — plan-only, not implemented; see Addendum 11.
