# Phase R-3 verification — 3 Aug 2026

The LLM strategist (`decide_site_strategy`) confirms or corrects R-1/R-2's
deterministic structural verdict once per source, cached in `SiteProfile`.
This is the layer that resolves the class of ambiguous page-shape judgment
R-2 explicitly deferred: short, generic-label marketing/CTA blocks that pass
every structural + syntactic filter but are not a company list
(`uni-augsburg.de`'s hero, `cdtm.de`'s hero, `campus-founders.de`'s program
list, `linkedin.com`'s topic pills — all named in `R2_VERIFICATION.md`'s
"Known limitation" section).

## Model deviation from the plan — 7B fails, 14B succeeds (live evidence)

The plan's literal instruction was to mirror `classify_startup` exactly:
`settings.ollama_extract_model` (7B, non-thinking). Built it that way first,
then live-tested it against two pages with **known-wrong** deterministic
verdicts from R-2 (`uni-augsburg.de` and `cdtm.de`, sample names like
`['Newsletter', 'Social Media', 'Imagefilm']` / `['Meet them', 'Become one',
'Explore them']`) — the 7B model **agreed with the wrong verdict both
times**, even after adding a concrete worked few-shot example to the prompt.

Re-tested the identical prompt against the 14B reasoning model
(`settings.ollama_reason_model`) directly — it correctly returned
`non_content` on both, with genuinely correct reasoning citing the specific
non-company-shaped names. Switched `decide_site_strategy` to a dedicated
`_site_strategy_client()` (14B, 240s timeout, `_strip_thinking()` applied
before JSON parse — required for Qwen3's `<think>` blocks, which the 7B
extraction model doesn't emit), `num_predict` 400→1500 and added
`num_ctx: 8192` (a tight cap can exhaust the whole budget on `<think>`
reasoning before the model reaches the JSON — same lesson already documented
for `recheck_record`). Re-tested all 4 known pages afterward — all correct.

**Justification for deviating from "mirror `classify_startup` exactly":**
this is a low-volume (once per source, cached `site_profile_ttl_days=30`),
genuine-judgment task — not a high-volume classification call. It matches
the project's own established two-tier philosophy (14B for judgment/dozens
of calls, 7B for volume/thousands of calls), not a violation of it.

## Position-based multi-profile parsing (bug found and fixed)

The schema returns 1–3 `(url_pattern, strategy)` pairs in one call — required
because a domain's listing and detail pages can need different handling
without a second LLM call. First implementation matched the "own page" entry
by string-comparing the returned `url_pattern` against the profiled URL's own
pattern. Live-tested on `schwaben.digital` (which has a detected `/events/*`
detail-link pattern, triggering a real 2-entry response): the model echoed
the prompt's human-readable placeholder text `"(domain default)"` literally
as the `url_pattern` value for its own-page entry (since the real pattern was
`""`, substituted with that placeholder for prompt readability). The string
match failed and silently fell back to `profiles[0]` by luck (this is a
single-profile-shaped bug that only manifests with a genuine multi-profile
response). Fixed by switching to pure position-based logic — position 1 in
the array is *always* "this page" by protocol, independent of what string
the model echoes — and rewrote the prompt's "Return: profiles" instructions
to state that explicitly.

## `DetachedInstanceError` on any page with a detail-link pattern (bug found and fixed)

Also found live-testing `schwaben.digital`. `probe_and_store` was calling
`_store_speculative_profile()` (which does its own `db.commit()` per
speculative entry) *after* the final `db.refresh(row)`. SQLAlchemy's default
`expire_on_commit=True` expires every object already loaded in the session on
any commit — including the just-refreshed `row` — so by the time the caller's
session closed, `row` was expired-then-detached, raising
`DetachedInstanceError` on first attribute access. Fixed by reordering:
speculative-profile writes now happen *before* the final refresh, not after.
Locked in with a regression test
(`tests/test_site_profile_store.py::test_row_survives_speculative_profile_storage_without_detaching`)
that specifically reproduces the ordering bug (asserts the fix, would fail
under the old order).

## Full 22-source batch re-profile with the 14B strategist

Re-ran `POST /sources/profiles/batch` (all 22 registered sources) with
`settings.site_strategy_llm_enabled=True`. 26 total profile rows (22 sources
+ 4 extra profiles from detected detail-link patterns: `zollhof.de`'s
`/startup-incubation/portfolio`, `schwaben.digital`'s `/events/*`,
`hochschule-biberach.de`'s bachelor-program pattern, `linkedin.com`'s
`/top-content/*`). 23/26 rows `strategy_source="llm"`, 2 `"llm_overridden"`
(deterministic's `text_extraction`/`chunking` won on high-confidence
disagreement — `page_shape` still took the LLM's corrected value), 1
`"deterministic"` (LLM call not needed/available for that row).

**Shape distribution, before (R-2, deterministic-only) → after (R-3, LLM-adjudicated):**

| Shape | R-2 count | R-3 count | Change |
|---|---|---|---|
| `unknown` | 5 | 3 | 2 DNS/cert-failure sources reclassified `non_content` by the LLM (same zero-extraction-expectation outcome, cosmetic label change — a known minor gap: the prompt's shape-option list doesn't describe `unknown`) |
| `logo_grid` | 2 | 2 | `linkedin.com` corrected out, `zollhof.de`'s real portfolio page (found via its own detected detail pattern) confirmed in |
| `card_directory` | 5 | 0 | all 5 corrected — see below |
| `non_content` | 0 | 10 | new shape value; absorbs the 5 corrected `card_directory` rows + 2 reclassified `unknown` rows + 3 pre-existing |
| `prose_listing` | 3 | 3 | unchanged |
| `article_feed` | 7 | 6 | `hochschule-biberach.de` split into `non_content` (listing) + `detail_page` (its real program pages) |
| `detail_page` | 0 | 2 | new — `hochschule-biberach.de` program pages, `schwaben.digital` `/events/*` |

## Every R-2 `card_directory` false positive corrected, none regressed

All 5 sources R-2 profiled as `card_directory` are re-examined below. This
includes the 2 the R-2 doc explicitly flagged as a known limitation deferred
to R-3 (`uni-augsburg.de`, `cdtm.de`), plus 3 more the LLM caught as a bonus
during the same batch without any additional targeted fixing
(`linkedin.com`, `tracxn.com`, `campus-founders.de` — `linkedin.com` was
R-2's `logo_grid`, not `card_directory`, but the same short-generic-label
failure mode):

| Source | R-2 verdict | R-3 verdict | LLM reason |
|---|---|---|---|
| `uni-augsburg.de` | `card_directory` (known limitation) | `non_content` | "sample names include UI labels, page copy, and asset labels (not distinct entities)" |
| `cdtm.de` | `card_directory` (known limitation) | `non_content` (`llm_overridden`) | "sample names are not distinct company names but action verbs and taglines" |
| `campus-founders.de` | `card_directory` | `non_content` (`llm_overridden`) | "Sample names indicate programs/initiatives (e.g., 'Founder Scholarship') rather than distinct company names" |
| `tracxn.com` | `card_directory` | `non_content` | "Sample names are categories (e.g., 'Venture Capital Funds') rather than distinct [companies]" |
| `linkedin.com` | `logo_grid` | `non_content` | "Sample names are categories (e.g., 'Career', 'Productivity'), not company names" |

`campus-founders.de` and `cdtm.de` landed as `strategy_source="llm_overridden"`:
the LLM's `page_shape` correction stands, but `text_extraction`/`chunking`
reverted to the deterministic high-confidence values per the override rule
(`page_shape` is never reverted by the override — only `text_extraction`/
`chunking`/`source` are). This is the override rule working as designed, not
a defect.

## Hard regression gates confirmed holding

| Source | R-1/R-2 verdict | R-3 verdict | Result |
|---|---|---|---|
| `zollhof.de` `/startup-incubation/portfolio` | `logo_grid`, 117 names | `logo_grid`, 117 expected, confidence `high` | **unchanged** — LLM confirmed, did not downgrade |
| `schwaben.digital` (default) | `article_feed`, 0 expected | `article_feed`, 0 expected, confidence `high` | **unchanged** — the 31 Jul junk-record incident path stays unreachable |
| `schwaben.digital` `/events/*` | not previously profiled | `detail_page`, speculative entry | new information, correctly separated from the listing page's own verdict |

The plan's HARD acceptance criterion — **"the LLM must never downgrade a
correct deterministic verdict on zollhof or schwaben"** — holds on every test
run including this full 22-source batch.

## Kill switch confirmed live

Tested `settings.site_strategy_llm_enabled=False` against a live probe:
`strategy_source` stayed `"deterministic"` and the LLM call was skipped
entirely, confirming the LLM is never load-bearing (Ollama down / malformed
response / kill switch all fall back cleanly to the deterministic verdict
already proven correct in R-1/R-2).

## Test suite

53/53 pytest green (43 from R-0/R-1 + 2 R-2 regression tests + 8 new R-3
tests: 4 in `tests/test_site_strategy_context.py` for the prompt-formatting
helper, 4 in `tests/test_site_profile_store.py` including the
`DetachedInstanceError` regression and a speculative-profile-never-overwrites
-a-real-one test). No database writes to `startups`/Qdrant at any point —
this phase only writes to `site_profiles`, still a table nothing in the live
crawl path reads yet (that's Phase R-4).

## Dashboard polish (same phase)

`shapeChip()` in `ui/static/js/views/sources.js` previously showed
"N expected" whenever `expected_entity_count > 0`, regardless of the final
`page_shape` — so a correctly-`non_content` row (e.g. `linkedin.com`,
`expected_entity_count=10` preserved as an honest structural fact from the
deterministic pass) still displayed a misleading count. Aligned it with
`PageStrategy.expects_entities` (`ingestion/strategy.py`): the chip now only
shows the count when `page_shape ∈ {logo_grid, card_directory, detail_page}`
*and* `expected_entity_count > 0`.
