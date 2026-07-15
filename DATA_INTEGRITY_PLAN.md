# Data Integrity, Provenance & Team Access — Build Plan

> **Origin:** Boss review after the successful newsletter run (2026-06). This plan covers the requested changes to how startup data is tracked, deduplicated, versioned, searched, and accessed — plus where the autonomous agent (OpenClaw/SCOUT) fits.
>
> **How to use this document:** Same convention as the main redesign plan — each phase is self-contained and handed to an implementer (Claude Sonnet 4.6) one at a time. Read §A Invariants first. Do not widen a phase's scope.

---

## ⚡ REVISION 2 (25 June) — New requirements, deadline 15 July

Meeting outcome. Four deliverables due at the 15 July review (~3 working weeks):

1. **Automatic deduplication of the database** (within 2 weeks) — both *ongoing* (skip-if-identical / version-if-changed, §Phase B) and a **one-time cleanup sweep** that finds and merges duplicates already sitting in the DB from earlier runs.
2. **Twice-weekly automatic pipeline runs** — replace the current schedule with two full sweeps per week (e.g. Mon + Thu early morning), running unattended (§Phase G.1).
3. **Dynamic sources (before holidays):** Fabian and Stefan must be able to add new websites and newsletters themselves. Hardcoded source lists (`ingestion/sources.py`, `config/source_registry.py`) are replaced by **one human-editable `config/sources.yaml`** loaded fresh at every run — no restart, no code change, no deploy. (Decision: **YAML over JSON** — supports comments for attribution ("# added by Stefan"), no comma/brace syntax traps for non-coders.)
4. **A frontend for the database** (§Phase G.3) — including an **"Add source" form** that writes into `sources.yaml`, so staff never have to touch a file at all.

**Guiding principle from the meeting:** make the system *dynamic instead of static* — anything an analyst might change (sources, keywords, trusted senders, schedule cadence) should be **data, not code**.

### New Phase S — Dynamic source registry *(~2 days, do first)*

**Goal:** sources become a live config file + API, editable in real time.

1. **Create `config/sources.yaml`** — one file, four sections: `rss_feeds`, `web_sources` (accelerators/universities/hubs with `name,url,type,region,priority`), `newsletter_senders`, `keywords`. Seed it by exporting today's hardcoded lists.
2. **Loader with validation** (`config/source_loader.py`): parse + pydantic-validate on every ingestion run (hot reload). A malformed entry is **skipped and logged, never crashes the run**; on unparseable file, fall back to the last-known-good copy.
3. **Rewire consumers:** `config/source_registry.py` and `ingestion/sources.py` become thin shims that read from the loader — downstream code (`rss_parser`, `scout_controller`, `web_scraper`) keeps its existing interfaces; nothing else changes.
4. **API:** `GET /sources`, `POST /sources` (validate → append to YAML), `DELETE /sources/{id}`. These power the frontend's "Add source" form and are also the future OpenClaw lever ("agent proposes a new source → human confirms → POST").
5. Keep the file under git so every source addition is version-tracked.

### One-time dedup sweep (part of req. 1)

`scripts/dedup_sweep.py`: scan all rows, group by fingerprint + fuzzy-match (≥ 88), merge duplicates through the same fill-empty-fields logic, keep the oldest row's stable UUID, append merged sources to `source_history`, delete the losers from PG + Qdrant. Dry-run mode first (`--dry-run` prints the merge plan); run for real only after review.

### Twice-weekly schedule (req. 2)

In `api/main.py`, replace the current interval jobs with cron-style triggers: **full sweep Mon + Thu 05:00** (RSS + web + newsletters through `scout_controller.run_all()`), still mutex-serialized. Gmail-only top-up stays daily (cheap, incremental).

### Revised 3-week roadmap to 15 July

| Week | Deliverables |
|---|---|
| **1 (to 2 Jul)** | Phase S (dynamic sources) · one-time dedup sweep · twice-weekly cron · Phase A (provenance: which newsletter, extracted-when, run id) |
| **2 (to 9 Jul)** | Phase B (skip-identical / version-changed — completes "automatic dedup") · Phase C (manual delete) · Phase D (filter + keyword search API) · `PATCH` edit endpoint |
| **3 (to 15 Jul)** | Phase G.3 frontend (search, filters, table, inline edit, delete, **Add-source form**) · Phase G.1/G.2 unattended hardening (launchd, no-sleep, Google app → Production) · full reboot rehearsal + buffer |

Everything in the older sections below still applies; this revision only re-orders and extends it.

### Folded in now (free — no extra time cost, done as part of the 3-week plan above)

These ride along with work already scheduled, so they ship by 15 July at no added cost:

- **Schedule cadence as config**, not a hardcoded number — done while converting to cron for the twice-weekly requirement (Phase S / week 1).
- **Newsletter keywords as config** — already part of `config/sources.yaml`'s `keywords` section (Phase S).
- **Dedup fuzzy-match threshold as config** — a single tunable value with a sane default, added while touching `deduplicator.py` for versioning (Phase B / week 2).

### Backlog — Phase S-2: Tunable extraction/scoring config *(deferred until after the vacation, ~2–3 days)*

**Explicitly deferred** — real new scope, and the risk of destabilizing extraction precision right before the 15 July demo / month-long unattended run isn't worth it. Revisit once the owner is back and the core system has proven stable for a month unattended.

1. **Extraction include/exclude rules** — move the industry inclusion/exclusion list out of `reasoning/prompts.py` into config, so business decisions (e.g. "start tracking healthtech") don't require a prompt edit.
2. **Candidate filter keywords/thresholds** — move `ingestion/candidate_filter.py`'s relevance heuristics into config; directly affects extraction volume and precision, so needs care + re-validation via the harness (§A.7) after any change, not a same-week rush.
3. **Scoring weights & tier thresholds** — `processing/scorer.py`'s definition of "PRIORITY" vs "WEAK_SIGNAL" is a judgment call, not an engineering one. Needs a conversation with the boss about what actually matters to them before encoding it — don't guess defaults under deadline pressure.

**Trigger to revisit:** first check-in after the owner returns from vacation, once Phase G's unattended operation has a month of real evidence behind it.

---

## A. Invariants (carry over from the main plan)

- **All LLM inference stays local (Ollama).** No cloud LLM calls, ever. (Privacy/security requirement.)
- **Two-tier models:** `qwen2.5:7b-instruct` for extraction, `qwen3:14b` for reasoning, `nomic-embed-text` for vectors.
- **One heavy LLM job at a time** via the `ScoutController` GPU mutex.
- **Every startup is written through `processing/storage.py::upsert_startup()`** — never bypass it.
- **Config lives in `config/__init__.py`** (not the shadowed root `config.py`).

---

## B. What the boss asked for → what already exists → what's missing

| # | Boss requirement | Already built | Work remaining |
|---|---|---|---|
| 1 | **Precise source** — not just "newsletter" but *which* newsletter, from where, so a human can verify | `source_history` JSON appends every source; `NewsletterEntry` stores sender + subject | Thread the **newsletter name, sender address, and subject** into the startup's `source_history` entry. Right now it only records `source="newsletter"` + `gmail://<id>`. |
| 2 | **Short summary** — enough to know what the startup does | `short_description` (one-liner) now extracted; `description` (2-3 sentences); `ai_summary` column exists | Mostly done. Polish: guarantee every record has a usable summary; optionally generate `ai_summary` for high-value leads. |
| 3 | **Dates** — when pipeline ran, when data was extracted | `created_at`, `updated_at`, `last_enriched_at`, `published_at` all exist; `source_history` has a `date` field | Add an explicit **extraction timestamp** + **run id** to each source_history entry so you can trace "this field came from this run on this date." |
| 4/5 | **Team-accessible DB with filters + semantic/keyword search** | Semantic search via Qdrant (`/scout/search`); basic filters on `/scout/list` (industry, country) | Add **rich filters** (tech_cluster, employee_count, funding_stage, date ranges, source, score tier) + **keyword text search** across name/summary. **UI is a separate decision — see §D.** |
| 6 | **No duplicates; mark & version changes; skip if identical** | Fingerprint dedup + fuzzy name match already collapse the same startup to one row; `_fill_empty_fields` fills NULLs without overwriting | **New:** content-hash to detect *identical* re-extraction → skip entirely. When fields *differ* → record a **version/change-log** entry (what changed, when, from which source) and update. This is the biggest new piece. |
| 7 | **Lifetime retention + manual delete** | Data is retained indefinitely by default | **New:** a manual **DELETE** endpoint that removes from PostgreSQL *and* Qdrant, with a confirmation/safety guard and an audit log. |
| 8 | **Room for OpenClaw / autonomy** | `ScoutController` mutex + targeted-ingest command surface already designed as the agent's lever | Keep architectural seams; the agent itself is the later ElizaOS phase. |

**Bottom line:** ~60% of the boss's request is already scaffolded. The genuinely new work is: change-detection/versioning (#6), manual delete (#7), and richer search/filter (#4/5), plus the UI decision.

---

## C. Phased plan

### Phase A — Precise provenance & timestamps  *(~1 day)*
**Goal:** every startup row can be traced to the exact source and run.

**Read first:** `processing/storage.py`, `ingestion/newsletter_ingestor.py`, `database/models.py`.

1. Extend the `source_entry` dict written to `source_history` to include:
   `{"source": "newsletter", "source_name": "<sender display name>", "sender": "<email>", "subject": "<subject>", "url": "gmail://<id>", "extracted_at": "<ISO ts>", "run_id": "<controller run id>"}`.
   For web/RSS, populate `source_name` with the human label from the source registry.
2. Pass sender/subject/run_id from `newsletter_ingestor` → `upsert_startup()` (extend its signature with an optional `provenance: dict`).
3. Ensure the web/RSS callers also pass a readable `source_name`.

**Done when:** a startup from a newsletter shows the newsletter's name, sender, subject, and extraction date in `source_history`; a human can open the email and verify.

---

### Phase B — Change detection, dedup skip & versioning  *(~2–3 days — the core ask)*
**Goal:** identical re-extractions are skipped; changed data is versioned and labelled.

**Read first:** `processing/storage.py::upsert_startup`, `processing/deduplicator.py`, `database/models.py`.

1. **Add a content hash** — compute `content_fingerprint = sha256(canonical(startup_fields))` over the *meaningful* fields (name, description, industry, funding, location, etc., excluding volatile ones like timestamps). Store it on the row.
2. **Skip-if-identical:** in `upsert_startup`, when an existing record's `content_fingerprint` equals the incoming one → **do nothing** (don't touch `updated_at`, don't re-score, don't re-embed). Return a new status so metrics can count `unchanged_skipped`.
3. **Version-if-changed:** when the incoming record differs, compute a **field-level diff** (old → new). Instead of silently overwriting:
   - Append a **version entry** capturing `{version_no, changed_at, run_id, source, changes: {field: {old, new}}}`.
   - Apply the update (respecting the "don't clobber good data with empty" rule).
   - Mark the row (`has_revisions = true`, `last_change_summary`).
4. **Storage choice for versions** (pick one, note it in the PR):
   - **Lightweight:** a `change_log` JSON column on `startups` (simplest, good enough for audit).
   - **Full history:** a `startup_versions` table (one row per version — queryable, heavier). *Recommended if the team will actually browse history.*
5. Migration script for the new columns/table.

**Done when:** re-ingesting the same unchanged newsletter stores **0** changes; re-ingesting a startup whose description or name changed creates a labelled version entry with the exact diff; the current record always reflects the latest, and the history is retrievable.

---

### Phase C — Manual delete with audit  *(~0.5–1 day)*
**Goal:** a team member can permanently delete one startup.

**Read first:** `api/routes/scout.py`, `vector_db/qdrant_store.py`, `database/models.py`.

1. Add `qdrant_store.delete_startup(id)`.
2. Add `DELETE /scout/startup/{id}` — removes the PG row **and** the Qdrant point, writes an audit record (who/when/what — even if "who" is just an API note for now), returns confirmation.
3. Safety: require an explicit confirm flag or return the record first for review.

**Done when:** deleting an id removes it from both stores; a second search no longer returns it; the deletion is logged.

---

### Phase D — Search & filter API (+ UI decision)  *(API: ~1–1.5 days; UI: see fork)*
**Goal:** team members find startups by structured filters or by remembering a snippet.

**Read first:** `api/routes/scout.py`, `vector_db/qdrant_store.py`.

1. **Rich filters** on `/scout/list`: `tech_cluster`, `employee_count`, `funding_stage`, `country`, `industry`, `source`, `score_tier`, `founded_year` range, `created_at`/`extracted_at` date range.
2. **Keyword search:** a `q=` param doing case-insensitive match across `name`, `short_description`, `description`, `tags` (Postgres `ILIKE` / full-text).
3. **Semantic search** already exists (`/scout/search`) — expose it consistently and allow combining with filters.
4. **Pagination + sort** (by score, date, name).

**→ UI fork (§D below) is a separate decision that changes the estimate significantly.**

**Done when:** you can query "climate startups in Munich, 11–50 employees, seed stage" via filters, and "that packaging startup that did X" via keyword/semantic search.

---

### Phase E — Autonomy layer (OpenClaw / SCOUT agent)  *(~3 days, highest uncertainty)*
This is the existing **Phase 4** from the main plan — the gap-filling research brain. It sits *on top of* the clean data layer built above. See §E for the ElizaOS explainer. Deferred until A–D are solid, but every phase above leaves seams for it (the controller command surface, run ids in provenance, status polling).

---

### Phase F — E2E test + runbook update  *(~1 day)*
Full sweep, failure injection (kill Ollama, expire token, delete during a run), confirm dedup/versioning under real load, document operations.

---

## D. The UI decision (this drives the estimate)

"Database accessed by team members with filters and search" can mean three very different things:

| Option | What it is | Effort | Trade-off |
|---|---|---|---|
| **1. API + docs only** | Team uses the FastAPI `/docs` page + the Discord bot | **0 extra days** | Works today; not friendly for non-technical team members |
| **2. Lightweight internal tool** | A simple admin page (e.g. Streamlit or a small static React table) hitting the API — search box, filters, table, delete button | **~2–3 days** | Good enough for an internal team; fast to build; local-only |
| **3. Full web dashboard** | Proper React app + auth/login + hosting + polished UX | **~5–8 days** | Real product; needs auth, deployment, maintenance |

**Recommendation:** Option 2. It gives the team a real searchable interface (filters + keyword + semantic + manual delete) without the cost and maintenance of a full app, and it stays local like the rest of the stack.

---

## E. Realistic total estimate

| Scope | Effort |
|---|---|
| Phase A — Provenance & timestamps | ~1 day |
| Phase B — Dedup skip + versioning (core ask) | ~2–3 days |
| Phase C — Manual delete | ~0.5–1 day |
| Phase D — Search/filter **API** | ~1–1.5 days |
| **Data-integrity core (A–D API)** | **~5–7 working days (≈1.5 weeks)** |
| UI Option 2 (internal tool) | +2–3 days |
| UI Option 3 (full dashboard) | +5–8 days |
| Phase E — Autonomous agent (ElizaOS) | ~3 days (high uncertainty) |
| Phase F — E2E + runbook | ~1 day |

**Honest headline for the boss:**
- The **data quality + versioning + search + delete** requirements (the heart of the request) = **~1.5 weeks** of focused work.
- Add a **usable team UI** (Option 2) = **~2 weeks total**.
- The **autonomous agent** on top = **~1 more week**, kept last because it's the least certain.

**Is it achievable?** Yes, comfortably. Most of it extends code that already exists rather than building from scratch. The one genuinely new and careful piece is change-detection/versioning (Phase B) — worth doing well because it's the foundation of "lifetime, trustworthy data."

---

## G. Phase G — Run unattended for a month + team UI  *(~3–4 days)*

**Context:** The owner is away for a month. The Mac mini stays powered on in the office; non-technical staff access the database over the office Wi-Fi from their browsers and can view, search, edit, and delete startups. The daily schedule must keep running with nobody touching the terminal.

**The problem today:** everything runs *inside* a manually-started process (`uvicorn`, the APScheduler jobs, the Discord bot). Close VS Code → it all stops. This phase makes it survive reboots, crashes, and a month of absence.

### G.1 Make the backend an always-on service
1. **API + scheduler as a `launchd` service** — a `~/Library/LaunchAgents/com.vcscout.api.plist` that runs `uvicorn api.main:app --host 0.0.0.0 --port 8000`, with `KeepAlive=true` (auto-restart on crash) and `RunAtLoad=true` (start on boot). The APScheduler jobs re-arm automatically on each startup (they're registered in the app lifespan), so the daily schedule survives restarts.
2. **Prevent the Mac from sleeping** — set Energy settings to never sleep on power, and run the service under `caffeinate -s` so ingestion never pauses.
3. **Docker containers** — set `restart: unless-stopped` in `docker-compose.yml` for `vc_postgres` and `vc_qdrant`, and set Docker Desktop to "Start on login." So the databases come back after any reboot.
4. **Ollama** — ensure it runs as a login service (the Ollama app / `brew services`) so the models are always available.
5. **Bind to `0.0.0.0`** (already the case) and note the Mac mini's LAN IP; confirm the office firewall allows the port on the local network only.

### G.2 Keep Gmail alive for the whole month  ⚠️ CRITICAL
- The Google Cloud OAuth app is in **"Testing"** mode → refresh tokens **expire after 7 days**. Gmail ingestion would silently stop ~1 week into the vacation.
- **Fix:** publish the app to **"Production"** in the Google Cloud console (OAuth consent screen → "Publish app"). This yields a long-lived refresh token. Re-run the one-time browser auth after publishing so a Production token is saved. **This is a manual settings change — do it before leaving.**

### G.3 Simple editable team UI (Streamlit — recommended)
Non-technical staff need a browser page, not the API docs or Discord.
1. Build a **Streamlit** app (`ui/app.py`) — pure Python, matches the stack, serves on the LAN (`streamlit run ui/app.py --server.address 0.0.0.0`). Run it as a second `launchd` service.
2. Features for staff:
   - **Search** box (keyword + semantic) and **filters** (tech_cluster, employee_count, funding_stage, country, source, score tier, date range) — powered by the Phase D API.
   - **Table view** with the summary, source (which newsletter, when extracted), and all key fields.
   - **Inline edit** via `st.data_editor` → writes back through a new **`PATCH /scout/startup/{id}`** endpoint (manual edits are flagged as `source="manual-edit"` in the version log from Phase B, so human changes are traceable).
   - **Delete** button → the Phase C delete endpoint, with a confirm step.
3. Add the **`PATCH /scout/startup/{id}`** update endpoint (part of this phase — needed for "staff can edit").

### G.4 A one-page "if it looks broken" guide for staff
A plain-English printout: how to tell it's running, and the *one* command to restart everything if the office loses power (`docker compose up -d` + the launchd services auto-restart). No coding required — just a checklist.

**Done when:** you reboot the Mac mini, log out, and without touching a terminal: the API is up, the daily schedule runs, Gmail still ingests (Production token), and staff can open the Streamlit page over Wi-Fi to search/edit/delete. Simulate this **before** leaving.

---

## H. Revised bottom line

| Scope | Effort |
|---|---|
| Data core (provenance, versioning, delete, search API) — Phases A–D | ~1.5 weeks |
| Team UI + unattended operation — Phase G | ~3–4 days |
| **Deliverable the boss wants (running DB + editable team access, unattended)** | **~2–2.5 weeks** |
| Autonomous agent (later) — Phase E | ~3 days |
| E2E + runbook — Phase F | ~1 day |

**You have ~4 weeks before leaving → this is comfortably achievable with buffer for testing.** The single most important non-code task is **publishing the Google app (G.2)** — without it, Gmail dies a week in.

---

## F. Where OpenClaw / autonomy fits (seams to preserve)

- Every write records a `run_id` + `extracted_at` → the agent can later reason about freshness and gaps.
- The versioning log gives the agent a signal for "this startup changed — worth re-analyzing."
- The delete endpoint + targeted-ingest command surface are the agent's future levers (under human confirmation — propose-then-act).
- Nothing in A–D blocks the agent; it consumes the same clean API the team uses.
