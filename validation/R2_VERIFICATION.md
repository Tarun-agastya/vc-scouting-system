# Phase R-2 verification — 3 Aug 2026

Batch-profiled all 22 registered web sources (`POST /sources/profiles/batch`,
zero LLM calls, zero DB writes to `startups`/Qdrant — pure structural
inspection). This is the "22 real sources at zero extraction cost" check the
plan calls for. Final distribution:

| Shape | Count | Sources |
|---|---|---|
| `unknown` | 5 | allgaeu-digital.de, arena2036.de, funkenwerk.tha.de, gruenderschmiede.kit.edu, xpreneurs.io |
| `logo_grid` | 2 | baystartup.de, linkedin.com |
| `card_directory` | 5 | campus-founders.de, cdtm.de, tracxn.com, uni-augsburg.de, zollhof.de |
| `prose_listing` | 3 | crunchbase.com, cyberlab.eu, example.com (placeholder entry) |
| `article_feed` | 7 | hochschule-biberach.de, munich-startup.de, sce.de, schwaben.digital, startbase.de, startup-autobahn.com, techfounders.com |

## Human-verified against the real pages

**The 5 `unknown` results are pre-existing DNS/certificate failures**, not an
inspector gap — confirmed against `logs/api.error.log` entries from as early
as 20-23 Jul (`ERR_CERT_COMMON_NAME_INVALID` on xpreneurs.io,
`ERR_NAME_NOT_RESOLVED` on the other four). The inspector's fallback for a
failed fetch is `unknown/full_text/expected=0` — today's exact behaviour,
never worse.

**Both `logo_grid` results are correct in different ways:**
- `baystartup.de` (`div.winner-logo`) — **true positive**, real companies:
  air up, Boxcryptor, Celonis, ChargeX, EGYM, Exasol.
- `linkedin.com` (`li.explore-top-content__pill`) — a **known false
  positive**: these are topic tags ("Career", "Finance", "Leadership"), not
  companies. Zero real-world impact — LinkedIn is `intelligence_platform`/LOW
  priority/`enrichment_only`, never crawled for company discovery by
  `run_all`/`run_accelerators`/`run_universities`. Deferred (see below).

**7 of 7 `article_feed` results are correct** — every one is a genuine
news/events list, not a company directory, confirmed by reading their sample
names (funding-round headlines, event titles, academic program links).
**This is the exact failure mode that wrote 72 junk records on
schwaben.digital on 31 Jul — now correctly rejected before it can happen
again**, generalizing to 6 other sources that were never tuned for.

**`card_directory` results are the registered entry point, not necessarily
the real content page.** zollhof.de's own `primary_url` is its *homepage*
(5 promo cards), not the 120-company portfolio page BFS discovers mid-crawl —
this is expected and already documented in `R0_BASELINE.md`; per-page
profiling (not just entry-point profiling) is Phase R-4/R-6's job. Two of the
five are homepage hero/CTA sections, not real content
(uni-augsburg.de, cdtm.de) — see "known limitation" below.

## Three real bugs found and fixed via this verification pass

1. **Breadcrumb navigation misclassified as a company grid** —
   `uni-augsburg.de`'s `<ol class="breadcrumbs">` (no `<nav>` tag, no ARIA
   role) scored 0.65, comfortably above the 0.55 card threshold, with names
   `['Universität', 'Organisation', 'Einrichtungen', 'Startseite', ...]`.
   Root cause: `\bbreadcrumb\b`'s trailing word-boundary cannot match inside
   the plural "breadcrumbs" (both `b`/`s` are word characters, no boundary
   between them) — a generic regex bug, not specific to this one site.
   Fixed the hint pattern to stem-match. That alone wasn't enough — a
   breadcrumb trail's OTHER signals (fully linked, all-distinct hrefs,
   homogeneous) score ~0.90 on their own, so the existing -0.25 penalty still
   cleared threshold. Changed the nav/footer penalty from a subtraction to a
   hard score CEILING (0.15) — page chrome now structurally cannot win
   regardless of how grid-like it looks.
2. **Partner/academic logos with "X logo" captions treated as company
   names** — `cdtm.de`'s partner-university strip yielded `['MIT logo',
   'cambridge logo', 'harvard logo', ...]`. `_ALT_NOISE` only exact-matched
   the bare word "logo"; the far more common "\<Name\> logo" accessibility
   convention slipped through whole. Added a `\blogo\b` substring check to
   the junk-alt regex — also cleans up Zollhof's own "Zollhof logo"/"Zollhof
   tech logo" entries as a bonus (previously reached the LLM as name-batch
   chunks and were correctly declined there; now filtered before extraction
   is even attempted, one fewer wasted call).

Both fixes are locked in as regression tests
(`tests/test_site_inspector.py::test_breadcrumb_trail_is_not_an_entity_group`,
`::test_partner_university_logos_are_filtered`) and verified live against
the real pages, not just synthetic HTML.

## A third fix attempted and reverted — worth recording why

Tried rejecting headline-shaped text at the per-item name-extraction stage
(not just the whole-group level) to catch `uni-augsburg.de`'s one long
descriptive sentence among otherwise-short hero labels. Live-tested and
**made things worse**: filtering headlines at the item level strips most of
a REAL news feed's names too (headlines are its actual content), which lowers
that group's own `frac_unique_name` enough that an unrelated, cleanly-named
decoy group can outscore it. Concretely broke `startbase.de`: its real news
feed lost the scoring race to a **browser-compatibility widget**
("Mozilla Firefox", "Google Chrome", "Safari", "Microsoft Edge") that has
nothing to do with the page's content. Reverted. The group-level
`frac_headline_names` check (computed from *unfiltered* names) is the correct
place for this signal and was never touched.

## Known limitation — deferred to Phase R-3, not further hand-tuning here

Marketing/CTA/hero-section blocks with **short, non-headline-shaped, generic
labels** still pass every structural + syntactic filter available:
`uni-augsburg.de`'s hero (`['StartHub-Titelbild', 'Newsletter', 'Social
Media', 'Imagefilm']`), `cdtm.de`'s hero (`['Meet them', 'Become one',
'Explore them']`), `campus-founders.de`'s program list (`['Corporate Campus
Challenge', 'AI Start', 'Incubator', 'Founder Scholarship']`), and
`linkedin.com`'s topic pills. None of these are long enough, sentence-shaped
enough, or nav/footer-hinted enough to fail on structure or syntax alone —
distinguishing a proper-noun company name from a short common-word UI label
needs either a curated stopword list (fragile, language-specific, doesn't
generalize) or semantic judgment.

**This is deliberately not chased further with more heuristics.** It is
exactly the ambiguous-case adjudication Phase R-3's cached LLM strategist
exists for — the prompt is designed to "confirm or correct" the
deterministic verdict, and a model shown `["Meet them", "Become one",
"Explore them"]` will trivially recognize it is not a company list, far more
reliably than another regex would. Real-world impact today is limited: none
of this feeds extraction yet (R-4), and the two HIGH-priority sources
affected (uni-augsburg, cdtm) are being profiled on their *homepage* — their
actual startup listings, if any, are separate pages BFS discovers and
profiles independently once R-4/R-6 land.

## Regression gates confirmed holding

| Source | Result |
|---|---|
| zollhof.de portfolio page | `logo_grid`, 117 names, unchanged from R-1 |
| schwaben.digital | `article_feed`, 0 expected — the 31 Jul incident path is not reachable |

45/45 pytest green throughout (43 from R-0/R-1 + 2 new regression tests).
No database writes to `startups`/Qdrant at any point — `site_profiles` is a
separate table nothing in the live crawl path reads yet.
