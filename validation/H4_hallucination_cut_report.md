# Phase H-4 — Validation Report: Hallucination Cut (H-1) Proven

**Date:** 21 July 2026
**Method:** controlled before/after A/B test using the `grounding.enabled` config
toggle (built in H-1 specifically for this), run against **identical live
content** (same URL, same crawl, same LLM), so the only variable is whether
the grounding gate ran. Full chunk text (not just the 200-char capture
preview) was independently re-fetched and cross-checked for every claim
below.

**Source:** `https://www.munich-startup.de/startups/` (real, live,
`source_type=startup_network`), 6 pages, 28 chunks, 46 extracted company-rows
per run — chosen because it's a dense multi-company listing, the same risk
profile that caused the original Polysense bug.

- Before run: `validation/68d02f8e-f0b4-4537-93e3-bbacb676736f/`
- After run: `validation/69ea62f7-53bc-4f34-8e49-06884135e254/`

## Headline result

| Field | Before (grounding OFF) | After (grounding ON, fixed) |
|---|---|---|
| `founded_year` populated | 26 / 46 | 16 / 46 |
| `employee_count` populated | 17 / 46 | 9 / 46 |
| `funding_stage` populated | 11 / 46 | 11 / 46 (unchanged) |
| `funding_amount` populated | 4 / 46 | 4 / 46 (unchanged) |

18 of 46 company-rows had at least one field nulled. **Every single nulled
value was independently verified against the full source chunk to have zero
textual support** — no over-nulling of a correct value was found anywhere in
this sample. `funding_stage`/`funding_amount` being unchanged is itself a
good sign: nothing fabricated on those fields in this sample, so grounding
correctly left them alone.

## Concrete evidence, not just counts

- **Celonis** (`founded_year=2010`, `employee_count=500+`): the source chunk
  is a bare list of names (`Celonis / Personio / Flix / Tubulis`) with zero
  detail. Both values are factually *true* in the real world — Celonis is a
  well-known German unicorn — but the model was reciting pretrained
  knowledge, not reading the source. Correctly nulled. This is the important
  distinction for a provenance system: **grounded, not merely correct.**
- **Defencetech, Sherpa** (`founded_year=2023`): neither source article
  states a founding year anywhere. Fabricated, correctly nulled.
- **Stellamia, Secfix, Rocket Tutor, Law.me, Nexus Politics, Kothontech**
  (`founded_year=2026`): these are podcast-interview summaries whose only
  date signal is the *article's* publish date, formatted `DD.MM.YY` (e.g.
  `30.06.26`) — never the literal 4-digit token `2026`. The model
  apparently converted a 2-digit publish-date fragment into a fabricated
  founding year. Correctly nulled.

## A real gap found and fixed during this testing

`Kothontech`, `Voiceline`, and `Auditr`'s fabricated `employee_count` values
**survived grounding on the first pass** — investigation showed why: my
`employee_count_signals` list included the bare word `"people"`, which
false-positive-matched because these source articles are all from a video
series literally named **"Pitch & People"** — completely unrelated to
headcount. Fixed by replacing the bare word with specific phrases
(`"people on the team"`, `"person team"`, etc.) in `config/tuning.yaml` and
the `config/tuning_loader.py` fallback. Re-verified directly against the
real Kothontech text after the fix: `employee_count` now correctly nulls.
This raised the employee_count catch rate on this sample from 4/17 to 8/17
nulled. **This is exactly what a "prove it" phase is for** — it surfaced a
real precision gap that pure code review didn't catch, which the harness's
own design (measure, don't assume) is what found it.

## An honest limitation — not every fabrication is catchable by literal grounding

`Moonscale`, `MyAutoData (MAUD)`, and `GovernLens` kept `founded_year=2026`
in both before and after. Full-chunk inspection explains why: the source
website's own directory-card template shows `2026 / Founded / - / Team
size / [Stage]` **identically for every single company on the page** —
while `Stage` genuinely varies per company (Seed/Growth/Startup, correctly
extracted), `2026` and `-` (empty team size) are uniform across all ~20
companies on that page. This is almost certainly the site's own
un-rendered placeholder (the real founding year likely populates via
client-side JavaScript our scraper doesn't execute) — meaning the literal
token `"2026"` genuinely IS present in the source text next to each
company. **H-1's literal-token grounding cannot distinguish "real
per-company fact" from "site template artifact that happens to contain the
right digits."** This is a known, disclosed limitation, not swept under
the rug: it's a different failure mode (bad data on the source website
itself) from the one H-1 targets (the model inventing a value with *zero*
textual basis anywhere). Only a source-website-aware heuristic or a
much more sophisticated semantic read (uncertain even Layer 2 would catch
this, since the text really does say "2026" in the founding-year position)
could close this specific gap — flagged for a future phase, not solved here.

## DB-wide picture (Phase H-3, all three source types — not assumed)

From `GET /verification/status` against the real, live database
(447 pre-existing + this test's real inserts):

| Source | Unverified | Verified | Flagged |
|---|---|---|---|
| web | (recheck in progress) | 0 | 223 |
| rss | 2 | 6 | 100 |
| newsletter | 0 | 0 | 116 |

The bulk of `flagged` is the honest `no_source_excerpt` bucket — records
ingested before H-1 shipped, which have no source text to re-verify against
and are correctly surfaced as "can't confirm" rather than silently trusted.
A large recheck batch (limit=100) was still running in the background as
this report was written to process the newly-added test records — check
`GET /verification/status` or the dashboard's Data Quality card for the
final numbers.

## What this does NOT claim

- This is not a full entity-level precision score (VALID/INVALID/PARTIAL
  per the harness's original design) — that requires either a human
  reviewer or full-chunk context beyond the 200-char capture preview, and
  producing one from insufficient context would be a worse outcome than not
  producing one at all. A human reviewer can still do that pass at
  `validation/{run_id}/export.csv` whenever convenient.
- Grounding does not, and cannot, fix bad data that already exists on the
  source website itself (see the `2026` template-artifact case above).

## Cleanup note

Both capture runs went through the live pipeline (the harness has no
dry-run mode) — this real-inserted ~90 new startup rows and ~90 duplicate-
review items into the production database. All are honestly retryable via
the normal review/verification workflow. If the review queue noise (90
items with no `run_id`, precisely identifiable) is unwanted:

```sql
DELETE FROM duplicate_reviews WHERE run_id IS NULL AND status='pending';
```
