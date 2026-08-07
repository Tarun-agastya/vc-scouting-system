# Phase RC — Regional Company Register (50 km around Memmingen)

> Build plan, 6 Aug 2026. Separate from the VC-scouting pipeline and from the
> Instagram-insights module. Nothing here touches the `startups` table.

---

## 1. Context

GreenTech Hub maintains a membership-prospect list ("Potenzielle Mitglieder")
of regional companies — today a hand-built Excel sheet, last updated 2025. It
carries two halves:

| Company data | Outreach tracking |
|---|---|
| Name · Standort · Mitarbeiter · Branche · Kurzbeschreibung | Prio · Kontakt allgemein · Status · Phase · Wer hat Kontakt |

**The goal:** replace the manual gathering of the left half with an automated,
re-runnable pipeline, while keeping the right half as a working CRM the team
edits by hand. Scope: companies with **100–4000 employees within 50 km of
Memmingen**. Priority fields are **Standort, Mitarbeiter, Branche,
Kurzbeschreibung** — 3 of 4 reliably is acceptable, 4 of 4 is the target.

The Excel sheet is a *reference sample*, not the target dataset. Its real value
is as a **ground-truth set for measuring recall** (see §3).

---

## 2. What was measured (6 Aug, live, read-only)

### 2.1 The existing list is not a radius

Geocoded all 61 rows via Nominatim and computed great-circle distance:

- **49 inside 50 km**, 12 outside.
- Outside: Wilhelm Geiger (69 km, 4000 MA), Lechwerke Augsburg (68 km),
  Otto Bihler (67 km), PMG Füssen, Baumit, Cooper Standard, Rba Lindau,
  DMG Mori Pfronten, BHS-Sonthofen, Rose Plastic, Schmid Simmerberg.
- **Max Bögl "Segenthal" is 167 km away** — that's Sengenthal near Neumarkt.
  A straight data error in the sheet.
- All 42 in-scope rows that carry an employee count fall inside 100–4000.
  7 in-scope rows have **no** employee count at all.

The sheet is *Allgäu-shaped*, not *radius-shaped*: it reaches south into the
mountains (Oberstdorf, Füssen, Sonthofen — all outside), while the real 50 km
circle reaches **west into Baden-Württemberg** (Biberach, Laupheim,
Ochsenhausen — SÜDPACK's home — Bad Waldsee, Ravensburg, Ulm/Neu-Ulm) and
**north** (Krumbach, Illertissen, Bad Wörishofen). Those directions are almost
absent from the current list, so meaningful new coverage is expected there.

### 2.2 Free-source recall, measured against the 49

Matching uses the project's own `normalize_company_name` + rapidfuzz
`token_sort_ratio >= 85` — not naive substring, which inflated an earlier
estimate to a false 51%.

| Source | Alone | Marginal gain over the others |
|---|---|---|
| Wikidata SPARQL (`P159` HQ within 50 km) | **36.7 %** (18/49) | — |
| German Wikipedia regional categories | **20.4 %** (10/49) | +3 |
| OpenStreetMap / Overpass (named industrial sites) | **28.6 %** (14/49) | +8 |
| **Union of all three** | **59.2 %** (29/49) | |

Raw volume found: Wikidata 383 companies in radius (only 47 with an employee
count), Wikipedia 173 articles, OSM 1085 named sites.

### 2.3 Why the 20 misses are missed — the structural diagnosis

The still-missing companies are **not obscure**: Wanzl, HAWE, Endress+Hauser,
3M Ceramics, Abt Motorsport, Swoboda, Myonic, Pester Pac. Three distinct
causes, and each implies a different fix:

1. **HQ-keyed sources lose plants and subsidiaries.** Wikidata keys on `P159`
   (headquarters). 3M's HQ is in the USA, Endress+Hauser's in Switzerland —
   both employ hundreds inside this radius, neither is findable by HQ. Fix: a
   **site-based** source. OSM is exactly that, and it is what recovered HAWE,
   Wanzl, Sensor-Technik Wiedemann, DILO and Josef Hebel.
2. **Notability-keyed sources lose family Mittelstand.** Kößler, SFB, Joma,
   Metzler Schaum, Regiogras have no encyclopaedia article and never will.
   Fix: a **registry-based** source (Handelsregister), or local-knowledge
   search.
3. **Neither indexes regional service businesses.** Autohaus Seitz, Dorr,
   Fristo, Bayernland, Allgäu Milch. Fix: local-knowledge search.

**Caveat, stated honestly:** the 49 are themselves a biased sample — they are
what a human already found, skewed toward larger and better-known firms. So
"59 % recall" means *"re-finds 59 % of what we already knew"*. True recall
against every company in the radius is unmeasured and probably lower. The
ground truth is a **lower-bound proxy and a comparator between sources**, not
an absolute score.

---

## 3. Getting from 59 % to 70–75 %

Levers in cost order. Each is measured against the same 49 before being
trusted — the discipline already used by `scripts/recall_test.py` and
`scripts/ig_probe.py`.

### Lever A — Per-municipality targeted search *(free, biggest expected gain)*

The 50 km circle covers ~80 municipalities across 8 Landkreise. For each,
run the **existing** `ingestion/web_search.py` (Tavily) with queries shaped the
way this information is actually published:

- `"größte Arbeitgeber {Landkreis}"`
- `"Unternehmen {Gemeinde}"` / `"Industrie {Gemeinde}"`
- `"Wirtschaftsförderung {Landkreis} Unternehmen"`

then extract with the **existing** local-LLM extraction path. This is how a
human would actually do it, and it directly targets all three miss-causes:
regional press "Top Arbeitgeber" listicles, IHK lists, and Wirtschafts­förderung
pages name exactly the family Mittelstand and the local service businesses the
structured sources cannot see.

Expected +15–25 pp → **~75–85 %**. This is an *estimate, not a measurement* —
Phase RC-3 exists to measure it before anything downstream depends on it.
Cost: ~80–150 Tavily searches, one-off, plus local LLM time.

### Lever B — IHK member directories *(free-ish, authoritative, access unknown)*

The radius spans **three chambers**: IHK Schwaben (Bavaria), IHK Ulm and IHK
Bodensee-Oberschwaben (Baden-Württemberg). IHK Schwaben's Memmingen office
lists 4,137 registered companies for Memmingen alone. Whether the member
database is exportable or only browsable is unknown and must be checked with
the chamber directly — **ask before scraping**; a member directory is exactly
the kind of asset whose terms matter, and GreenTech Hub's own relationship with
the IHK is worth more than the data.

### Lever C — Handelsregister API *(paid, the reliable close)*

Implisense (~2.9 M active German companies from the Handelsregister, with
employee counts, WZ industry codes and lat/lon), North Data, or OpenRegister.
Registry-based means **every registered GmbH appears regardless of notability**
— structurally fixing miss-cause 2. Expected 85–95 %.

Recommendation: **do not buy yet.** Run Lever A, measure, and let the number
decide. If A lands ≥70 %, the paid source is optional; if it lands at 62 %, the
spend is justified by evidence rather than assumption.

### Lever D — Google Places API *(paid but cheap; re-assessed 7 Aug)*

Initially rejected over the 60-results-per-query cap. That was too quick — the
cap is worked around by **grid tiling**: subdivide the circle into ~500–800
small overlapping search areas and query each. Re-checked pricing:

- **Essentials (IDs only): unlimited free.**
- **Pro** (displayName, address, location, types): **$32 / 1,000 calls** →
  roughly **$20–30 for one full sweep** of the radius.
- `includedTypes` supports 180 place types for filtering.

The real argument for it is coverage: German SMEs *self-register* a Google
Business Profile because they want to be found. A firm like Kößler Technologie
has no Wikipedia article and may not be in OSM, but almost certainly has a
Google listing. That directly attacks miss-cause 2. Still returns **no
employee counts**, so enrichment (RC-4) is unchanged.

Worth testing at RC-3 alongside Lever A, since one sweep costs less than an
hour of anyone's time.

### Lever E — OffeneRegister.de bulk Handelsregister *(free, needs verifying)*

The full German commercial register as **free bulk JSON/SQLite**, published by
Open Knowledge Foundation Deutschland with OpenCorporates. Registry-based, so
every registered GmbH appears regardless of notability — structurally the
strongest fix for miss-cause 2, at zero cost and with no rate limit once
downloaded locally.

**Caveat, measured 7 Aug:** the site is up but its query endpoint
(`db.offeneregister.de`) returns **502 Bad Gateway**, and the project's
freshness is unclear. Verify the bulk download still exists and check its
vintage *before* planning around it. Registry data gives name, legal form and
registered address — **not** employee counts or descriptions, so it is an
enumeration source, not a replacement for enrichment.

Related tooling if this route is taken: `bundesAPI/handelsregister` (CLI for
the official portal), `openregister-python`, `handelsregister.ai` (commercial).

### Explicitly rejected

- **Job-board scraping** (Indeed / StepStone / Kununu employer pages) — would
  give site-based employer data, but it is scraping a commercial platform
  against its terms. Same reasoning that ruled out Instagram scraping: not
  worth the exposure.

---

## 4. Accessibility — keeping this out of the startup haystack

> *"I don't want this data lost in our dashboard among thousands of startups."*

This is a first-class design constraint, not a UI afterthought. The startup DB
holds 1,206 records and grows continuously; ~500 regional companies dropped
into it would be unfindable and would also corrupt startup-scouting relevance
ranking. **Total separation, at every layer:**

| Layer | Decision |
|---|---|
| Storage | New table `regional_companies`. **Never** `startups`. No shared IDs. |
| Vectors | Its own Qdrant collection, or none at all — semantic search over SMEs is not needed for outreach. Must not pollute the startup collection. |
| API | New router `api/routes/regional.py`, mounted at `/regional`. |
| Dashboard | **Its own nav entry and page** — "Region" / "Mitglieder-Pipeline". Not a filter inside Browse. |
| Review | Its own review queue, or direct-apply with source citations — must not flood the startup Review Inbox. |
| Export | **CSV/XLSX export is mandatory**, not a nice-to-have. |

**Why export is mandatory:** the team works in Excel today and the sheet is
shared with people who will never open the dashboard. A pipeline that cannot
hand back an Excel file is a downgrade from what they already have, however
good the UI is.

### The page

One table, the sheet's own columns, in the sheet's own order — so it reads as
the familiar artefact, not a new tool:

`Name · Standort · Entfernung · Mitarbeiter · Branche · Kurzbeschreibung ·
Prio · Status · Phase · Kontakt · Wer hat Kontakt`

- Filters: distance band, employee band, Branche, Status, Prio, "has no
  employee count" (the work queue).
- Sort by distance ascending as the default — the nearest company is the most
  relevant partner.
- Every automatically-gathered value shows its **source URL** on hover (the
  same citation discipline as web-verify) so a human can check any number.
- The CRM columns (Prio/Status/Phase/Kontakt/Wer) are **human-only**, never
  written by any automated step.

---

## 5. Phases

### RC-1 — Table, import, geo scope *(foundation)*
- `regional_companies` table in `database/models.py`, `SiteProfile` house style:
  UUID pk, `created_at`/`updated_at`, `raw` JSON for provenance, indexes on
  `normalized_name`, `distance_km`, `employees`, `status`.
  Columns: the 5 data fields, the 5 CRM fields, plus `lat`/`lon`,
  `distance_km`, `source` (wikidata|wikipedia|osm|search|manual|sheet),
  `source_url`, `employees_source_url`, `last_verified_at`.
- `scripts/migrate_regional.py` — idempotent, `create_all(tables=[...])` pattern.
- Import the existing sheet (all 61, including the 12 outside 50 km — flagged
  `in_radius=false`, not deleted; they carry real contact history).
- Geocode via Nominatim, compute `distance_km`, set `in_radius`.
- **Fix the Max Bögl Sengenthal error** on import, with a log line.

**Verify:** 61 rows imported; 49 `in_radius=true`; distances match §2.1.

### RC-2 — Free-source discovery *(Wikidata + Wikipedia + OSM)*
- `regional/discovery.py` with one function per source, each returning a
  normalised candidate dict. Reuse the three probe scripts already written
  and validated in this session as the starting implementations.
- Dedupe candidates against each other and against RC-1's imports using the
  **existing** `normalize_company_name` + rapidfuzz — do not write a new matcher.
- **Reuse the Phase J institutional-junk filter.** The Wikidata sweep returned
  Sparkassen, Raiffeisenbanken, Volksbanken, BKK Verbundplus and Stadtwerke —
  precisely the class `_is_implausible_startup_name` already rejects.
- Store with `source` provenance; never overwrite a human-edited field.

**Verify:** recall against the 49 reproduces **59 %** (±3). Junk filter removes
the banks. New finds include Goldhofer, Otto Christ, Rohde & Schwarz
Messgerätebau, Liebherr-Verzahntechnik, Liebherr-Hydraulikbagger, Feneberg,
Honold Logistik, Baufritz — all real, all missing from the sheet today.

### RC-3 — Lever A: per-municipality search *(the recall push)*
- Enumerate municipalities in radius (Nominatim / OSM admin boundaries).
- Per municipality, run the existing Tavily search + local extraction.
- Hard per-run cap on outbound searches, following `web_verify_chain_limit`'s
  precedent — `ingestion/web_search.py` still has **no** quota accounting.
- **Measure recall against the 49 again, and report the delta.**

**Verify / decision gate:** if union recall ≥70 %, the free path is sufficient
and Lever C is dropped. If <70 %, present the measured number and the
Implisense cost so the buy/don't-buy call is made on evidence.

### RC-4 — Enrichment *(Mitarbeiter, Branche, Kurzbeschreibung)*
The part that is already built. `processing/web_verifier.py::web_verify_proposal`
takes a name + city, searches, and proposes field values **with source URLs**,
writing nothing. Point it at `regional_companies` instead of `startups`:
- Fill `employees` where null (7 known in-scope rows, plus most new finds).
- Fill `branche` and `kurzbeschreibung`.
- Refresh anything older than `rc_reverify_days`.
- Store the citation per field so the dashboard can show it.

**Verify:** the 7 in-scope rows with no employee count get one, each with a
working source URL a human can check.

### RC-5 — API + dashboard page + export
- `api/routes/regional.py`: list (filter/sort/paginate), detail, PATCH for the
  CRM columns, CSV/XLSX export.
- `ui/static/js/views/regional.js` + nav entry, per §4.
- Reuse `browse.js`'s CSV-export pattern; add XLSX only if plain CSV proves
  insufficient in real use.

**Verify:** a non-technical user filters to "Unterallgäu, 100–500 MA, Status
leer", edits a Status, and exports the result to Excel. No regional company
appears anywhere in startup Browse or search.

### RC-6 — Refresh cadence
- Discovery re-run: quarterly (the population barely moves).
- Enrichment re-verify: rolling, oldest-first, small daily batch.
- No new launchd service — extend the existing scheduler, or a single
  `com.gthub.regional` one-shot following the `press_monitor` pattern.

---

## 6. Sequencing and reuse

RC-1 → RC-2 → RC-3 (gate) → RC-4 → RC-5 → RC-6. Each independently shippable.

Roughly 60–70 % of this already exists and must be **reused, not rebuilt**:
`normalize_company_name` + rapidfuzz (dedupe) · Phase J junk filter ·
`ingestion/web_search.py` (Tavily) · the local-LLM extraction path ·
`web_verify_proposal` (enrichment with citations) · the migration-script
pattern · `browse.js` CSV export · Nominatim geocoding (validated today).

Genuinely new: the `regional_companies` table, the three discovery adapters,
the municipality enumerator, and the dashboard page.

---

## 7. Out of scope / deferred

- **Scraping IHK, job boards, or any commercial directory.** Ask the IHK
  directly instead.
- Contact-person discovery (names, emails, phone). The sheet has these, filled
  by hand; automated harvesting of named individuals' contact details is a
  GDPR question that needs a deliberate decision, not a side effect of a
  scouting pipeline. **Import and preserve what exists; do not auto-gather more.**
- Companies below 100 employees — genuinely close to invisible in every free
  source; revisit only if the band is widened.
- Semantic/vector search over regional companies. Not needed for outreach;
  keeps the Qdrant startup collection clean.
- Any automated write to the CRM columns.

---

## 8. Open questions for the owner

1. **Is GreenTech Hub an IHK Schwaben member?** Decides whether Lever B is a
   free phone call or a dead end.
2. **Budget for a Handelsregister API** if RC-3 lands below 70 % — roughly what
   order of magnitude is acceptable?
3. **Should the 12 out-of-radius companies stay visible** (they carry real
   contact history — BHS-Sonthofen already declined, Kolb had conversations)?
   Current assumption: keep, flag `in_radius=false`, filter out by default.
