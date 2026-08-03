# Phase R-6 verification — 3 Aug 2026

Detail-page following: a third, highest-priority frontier tier in
`_crawler_task` that follows per-company detail links harvested from
*inside* a name-only logo grid's own cards — never from any `<a href>` on
the page — so a stub record missing most fields can gain them from its own
detail page, bounded by the same `max_pages` budget (capped at
`settings.crawl_detail_page_share`, 0.6).

No currently-registered source both has a name-only grid AND real,
structurally-auto-detected per-company detail links (confirmed via
`GET /sources/profiles` before starting — `zollhof.de`'s 117-item grid has
**no hrefs on its cards at all**, matching the pre-existing memory note from
the original 31 Jul probe). All live tests below therefore deliberately
enable detail-following on the real `baystartup.de` domain-default profile
(its 6 real cards genuinely link to `/startupdate/*` pages — 2 with a
unique href, 4 sharing a generic "hall-of-fame" page, pulling
auto-detection coverage to 0.5, just under the 0.6 threshold — exactly the
kind of case a human "pin" or a future LLM adjudication would enable on
purpose). `upsert_startup` is stubbed (dry-run, no Postgres/Qdrant writes);
`follow_detail_links`/`detail_link_pattern` are saved and restored after
every test.

## Code changes

- **`config/__init__.py`**: `crawl_detail_page_share: float = 0.6`.
- **`ingestion/web_scraper.py`**: `PageContent.entity_links` — `(name, absolute_href)` per primary-group card, populated by `extract_content` for both `alt_harvest`/`card_structured` modes (the one place card membership is known — `_extract_links()` has no such scoping). New `_matches_detail_pattern(url, pattern)` helper. `_crawler_task` gained a third frontier tier (`frontier_detail`, drained first, budget-gated via `_detail_available()`/`_has_more()` so the while-loop's own condition never disagrees with what `_next()` can actually pop) and, after building a page's content, harvests `content.entity_links` matching `strategy.detail_link_pattern` into it when `strategy.follow_detail_links`. A visited detail page's text is prefixed `"Company: {name}\n\n..."` using the name literally harvested from the listing card (H-1 grounding applies verbatim — never invented).
- **`ingestion/worker_queue.py`**: `chunker_task` now sets `ChunkItem.entity_hint = item.parent_entity_name`.
- **`processing/site_profile_store.py`**: `get_profile(url, strict=False)` — `strict=True` skips the domain-default fallback, returning only an exact-pattern match or `None`.
- **`processing/scout_controller.py`**: `_work_web` now looks up the target URL's profile and passes `max_pages`/`max_depth` through to `scrape_source` when a profile sets them (both are always `NULL` today — no code path writes them yet — so this is a no-op in production until something does; the plan explicitly calls out that nothing passed these through before this phase).
- **New tests**: `tests/test_detail_pages.py` (12 tests) — `_matches_detail_pattern` (real subpath, bare-prefix rejection, unrelated path, missing wildcard, `None` pattern, prefix-must-match-exactly-not-substring), `extract_content`'s `entity_links` for both structural modes plus the no-primary-group and non-structural-mode empty cases, and `chunker_task`'s `entity_hint` propagation (present when `parent_entity_name` is set, `None` otherwise).

## One bug found and fixed via live testing: detail pages inherited the wrong strategy

First live pass against `baystartup.de` (detail-following enabled, `max_pages=10`): `detail_pages_followed=3` — the mechanism successfully discovered and visited all 3 real `/startupdate/*` URLs — but the extracted "companies" were garbage: `"Meilensteine"` (German for "Milestones", a section heading), `"Process Mining"` (a topic tag), `"Expansion und Wachstum"` (a section heading), `"Die Vision: Jeder Stellplatz ein Ladeplatz"` (ChargeX's own tagline, not a name).

Root cause: `get_profile(url)` falls back to the domain's default profile when no pattern-specific one exists — a deliberately convenient R-4 default for ordinary subpages that probably share their domain's shape. But a **detail page almost never shares its listing page's shape** — `baystartup.de`'s domain default is `page_shape=logo_grid, text_extraction=alt_harvest` (correct for the homepage's real 6-company grid), and every one of the 3 detail pages silently inherited that same `alt_harvest` extraction instead of getting classified on its own merits — so each detail page's *own* incidental card-like structure (a milestones timeline, a topics list) got harvested as if it were more portfolio-grid entries.

**Fix**: `get_profile()` gained a `strict` parameter; `_crawler_task` passes `strict=True` whenever `parent_entity_name is not None` (i.e. the page was reached via the detail-page frontier). A detail page now only ever inherits a cached strategy from an **exact** pattern match — never the domain default — falling through to the same fresh `store_deterministic` derivation an ordinary first-time page uses, so it gets classified from its own actual content every time.

**Result after the fix**: re-ran the identical scenario. The two genuinely-unique detail pages extracted **exactly and only** their own company — `baystartup-alumni-celonis` → `"Celonis"`, `baystartup-alumni-chargex` → `"ChargeX"` (×3, once per chunk of that page, all correctly attributed) — directly confirming the `parent_entity_name`/`"Company: {name}"` prefix mechanism works as designed. The shared `hall-of-fame` page, now correctly classified fresh (`page_shape=article_feed`, `text_extraction=main_prose` — its own repeating group links to `None`, i.e. no per-item href, so `_is_editorial`'s structural check correctly called it editorial rather than a directory), extracted 11 more real alumni companies as a bonus (air up, EGYM, Exasol AG, Fazua GmbH, FlixBus, Hotel.de, Hydrogenious Technologies GmbH, Magazino, NavVis, Quantum Systems, Temedica) alongside 2 minor junk fragments ("wickelt", "Drohnenproduzent") — consistent with, not worse than, the already-documented Phase J prose-extraction imperfection (Addendum 11), not a new R-6 regression.

## Live test results

| | First pass (bug) | After fix |
|---|---|---|
| `pages_crawled` | 10 (max_pages respected) | 10 (max_pages respected) |
| `detail_pages_followed` | 3 | 3 |
| Celonis's own page | `"Process Mining"` (wrong) | `"Celonis"` (correct) |
| ChargeX's own page | 3× section-heading junk | `"ChargeX"` ×3 (correct) |
| Shared hall-of-fame page | 3× `"Meilensteine"` (junk) | 11 real companies + 2 minor junk |
| Ordinary listing extraction (homepage) | unaffected | unaffected — still 6/6 real names |

`max_pages` was never exceeded in either run (`pages_crawled=10` exactly, the configured cap). `/health` showed `startups_in_db: 1412` unchanged before/after both runs (dry-run stub, no real writes). `follow_detail_links`/`detail_link_pattern` restored to their original `False`/`None` on the real `baystartup.de` domain-default profile after every test — confirmed via `GET /sources/profiles`.

**Side effect worth noting, not a bug:** the real R-5 recall audit fired during these live runs (as it correctly should on any real crawl) and left the domain-default profile's feedback columns (`last_expected`, `last_extracted`, `recall_ratio`, `consecutive_shortfalls=1`) reflecting real signal from a real fetch — not reset by the test's save/restore (which only covers the two fields this test deliberately changed). This is honest, harmless, self-correcting state from an actual crawl, not test pollution to clean up. A genuine new profile row was also persisted at `(baystartup.de, /startupdate/*)` — `page_shape=article_feed`, correctly capturing what R-6 now knows about that pattern for future runs; left in place rather than deleted, matching the "SiteProfile is an ever-growing learned cache" convention from every prior R-phase test.

## Test suite

99/99 pytest green (87 pre-R-6 + 12 new in `tests/test_detail_pages.py`).

## Scope not covered by this verification

- `scout_controller._work_web`'s new `max_pages`/`max_depth` override wiring is untestable live today — no `SiteProfile` row has ever had these columns set by any code path (schema-only until now). The wiring is in place for whenever a future phase (or a dashboard "set custom budget" control) populates them.
- A source where `follow_detail_links` is enabled by the real R-3 LLM adjudication rather than a deliberately-constructed test — none of the 22 registered sources currently qualify (structurally verified before starting). This is the natural target for R-7's generalization work on new, never-tuned sources.
- Detail pages nested more than one level deep (a detail page that itself links to further detail pages) — `depth + 1` is threaded through the detail frontier the same as the other two tiers, so the mechanism should compose, but wasn't specifically exercised (baystartup's detail pages are leaf pages).
