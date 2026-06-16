# VC Scouting System — Architecture Redesign & Phase-by-Phase Implementation Spec

> **How to use this document.** This is the build plan for an implementer (Claude Sonnet 4.6) who will be told *"implement Phase N"* in a fresh session. Each phase is self-contained. **Before implementing any phase, the implementer MUST read the two shared sections first: [§A Project Facts & Invariants](#a-project-facts--invariants) and [§B What Already Exists](#b-what-already-exists--do-not-rebuild).** Then read only the files named in that phase, make the changes, and run that phase's Verify steps. Do not start a later phase's work early. Do not refactor things a phase doesn't mention.

---

## Context (why this work exists)

A RAG pipeline scouts early-stage startups in the **DACH region + broader Europe** (plus internationals targeting that market). It crawls curated web sources, extracts structured startup profiles with a **local** LLM (Ollama), dedupes + scores them, and stores them in PostgreSQL + Qdrant for semantic search, matchmaking, and an analyst-facing chat agent ("OpenClaw"/SCOUT).

**Problem:** the pipeline overloads the Mac mini and fails. Earlier attempts ran the OpenClaw agent *and* the heavy `qwen3:14b` extraction pipeline on the same single Ollama backend at once, oversubscribing the GPU → cascading 120s timeouts (documented in `PERFORMANCE_ANALYSIS_QWEN_BOTTLENECK.md`).

**Intended outcome:** a system that (1) never overloads the machine, (2) properly wires **Gmail newsletter scouting** into the *same* dedup/scoring pipeline, (3) runs OpenClaw as the **decision-making brain** that commands ingestion (not a chat veneer, not the thing that grinds chunks), and (4) is delivered in small, independently-shippable phases.

**Privacy constraint (hard requirement):** all LLM inference stays **local** (Ollama). No cloud LLM calls. This is a deliberate choice for privacy/security around the agent.

---

## A. Project Facts & Invariants

> **Read this section before every phase. These are non-negotiable rules for the whole project.**

### A.1 Machine (measured)
- Apple **M4 Mac mini, 24 GB unified memory, 10 cores**. RAM is *not* the bottleneck.
- **Ollama server 0.30.7** running at `http://localhost:11434`. Installed models: `nomic-embed-text`, `qwen3:14b`.
- **Node v22.22.2** installed (needed for the ElizaOS agent in Phase 4).
- Docker: `vc_postgres` (healthy), `vc_qdrant` (**currently unhealthy — fixed in Phase 0**).

### A.2 The two-tier model rule (the heart of the redesign)
| Tier | Model | Used for | Volume |
|---|---|---|---|
| **Extraction (hot path)** | `qwen2.5:7b-instruct` (small, *non-thinking*, JSON-schema output) | parsing startups out of every chunk / email | thousands of calls |
| **Reasoning (cold path)** | `qwen3:14b` (existing) | agent judgment, `analyze_startup`, `synthesize_scout_results`, `generate_sector_report` | dozens of calls, on-demand |
| **Embeddings** | `nomic-embed-text` (existing) | 768-dim vectors | per upsert |

- **DO NOT** use `qwen3:14b` for chunk/email extraction. It is a *thinking* model — it wastes ~40–50s/chunk generating `<think>` blocks that get discarded.
- **DO NOT** use a cloud model anywhere.

### A.3 The GPU-mutex rule (what actually prevents the overload)
> **Only ONE heavy LLM job may touch Ollama at a time.** A single `asyncio.Lock` ("GPU mutex"), owned by the `ScoutController` (Phase 3), serializes: scheduled ingestion, agent-commanded targeted ingestion, and any agent 14B reasoning call. The small extraction model still runs one-request-at-a-time (`max_qwen_workers` governs this). The fix for the original crash is **arbitration, not reducing the agent's intelligence.**

### A.4 The "good" storage path — never bypass it
Every startup, regardless of source (web, RSS, newsletter), **MUST** be persisted via `processing/storage.py::upsert_startup()`, which does: fingerprint dedup (`processing/deduplicator.py`) → deterministic scoring (`processing/scorer.py`) → write PG `startups` row → embed → upsert Qdrant point keyed by the **stable fingerprint UUID**.
- **DO NOT** write points to Qdrant with a random `uuid4()` (this is the current newsletter bug — it breaks dedup and scoring).

### A.5 Config is centralized
- The authoritative settings object is `config/__init__.py` → `Settings` (pydantic-settings), imported as `from config import settings`.
- The root-level `config.py` is **legacy/shadowed** by the `config/` package — **do not edit `config.py`; edit `config/__init__.py`.**
- New tunables go on `Settings` with sane defaults + `.env` override.

### A.6 Async & concurrency conventions
- The ingestion pipeline is asyncio-based (`ingestion/worker_queue.py`). Ollama calls are **synchronous** and dispatched off the event loop via `run_in_executor` — preserve this pattern; never call Ollama directly on the event loop.
- Singletons exist and are imported by name: `reasoning/qwen_client.py::qwen_client`, `ingestion/newsletter_ingestor.py::newsletter_ingestor`, `vector_db/qdrant_store.py::qdrant_store`, `embeddings/embedder.py::embedder`. Reuse them; don't instantiate parallel copies.

### A.7 Validation harness exists — use it to prove extraction quality
`validation/` (capture.py → exporter.py → metrics.py) + `scripts/run_validation.py` (`capture` / `export` / `metrics`). Outputs land in `validation/{run_id}/`. Use it as the before/after gate whenever extraction behavior changes (Phases 1, 2).

### A.8 Key file map
| Area | File |
|---|---|
| Settings | `config/__init__.py`, `config/source_registry.py` |
| Extraction LLM client | `reasoning/qwen_client.py`, `reasoning/prompts.py` |
| Pipeline (4-stage async) | `ingestion/worker_queue.py`, `ingestion/pipeline.py` |
| Crawl / chunk / filter | `ingestion/web_scraper.py`, `ingestion/chunker.py`, `ingestion/candidate_filter.py` |
| RSS / sources | `ingestion/rss_parser.py`, `ingestion/sources.py` |
| **Gmail** | `ingestion/newsletter_ingestor.py` |
| Dedup / score / store | `processing/deduplicator.py`, `processing/scorer.py`, `processing/storage.py` |
| Embeddings / vectors | `embeddings/embedder.py`, `vector_db/qdrant_store.py` |
| DB | `database/models.py`, `database/connection.py` |
| API + scheduler | `api/main.py`, `api/routes/{scout,matchmaking,ingestion}.py` |
| Agent config | `openclaw/character.json`, `openclaw/tools_config.json` |
| Discord | `discord_bot/bot.py` |
| Scripts | `scripts/{run_ingestion,run_validation,setup_db,migrate_*}.py` |
| Infra | `docker-compose.yml`, `requirements.txt` |

### A.9 Global DO-NOTs (apply to every phase)
- ❌ No cloud LLM calls (privacy). ❌ No `qwen3:14b` for extraction. ❌ No bypassing `upsert_startup()`. ❌ No editing `config.py` (edit the package). ❌ Don't commit secrets (Discord token, Gmail creds). ❌ Don't run two heavy LLM jobs concurrently — route through the mutex once Phase 3 exists. ❌ Don't widen a phase's scope; if you find an unrelated bug, note it, don't fix it inline.

---

## B. What Already Exists — Do NOT Rebuild

Most "new" features are already partly built and need *fixing/wiring*, not greenfield work:

| Capability | Status | Where |
|---|---|---|
| 4-stage async ingestion (crawl→chunk→LLM→store) | works, slow/fragile | `ingestion/worker_queue.py` |
| Web crawler (httpx + Playwright fallback, BFS, domain-isolated) | works | `ingestion/web_scraper.py` |
| Heuristic pre-LLM filter (`is_relevant`) | works | `ingestion/candidate_filter.py` |
| Chunker (1,800-char overlap, sentence-snap) | works | `ingestion/chunker.py` |
| Fingerprint dedup + stable UUID | works | `processing/deduplicator.py` |
| Deterministic 0–100 scoring + tiers | works | `processing/scorer.py` |
| `upsert_startup()` (PG + Qdrant, the good path) | works | `processing/storage.py` |
| Embeddings (nomic-embed-text) | works | `embeddings/embedder.py` |
| **Gmail ingestion (OAuth2 + extract)** | **half-built; bypasses dedup/scoring; truncates to 3,500 chars; not scheduled** | `ingestion/newsletter_ingestor.py` |
| **OpenClaw agent** | **config files only — nothing executes them** | `openclaw/*.json` |
| FastAPI + APScheduler (RSS every 6h) | works | `api/main.py` |
| Validation harness (3-stage precision) | works | `validation/`, `scripts/run_validation.py` |

---

## Root causes of the failures (for orientation)
1. **Wrong model on the hot path** — `qwen3:14b` (thinking) for per-chunk extraction.
2. **Single backend oversubscribed** — 2 workers / agent + pipeline queue behind a 120s HTTP timeout that counts from submission.
3. **Agent coupled to the heavy loop** with no resource arbitration.
4. **Newsletter path bypasses the good pipeline** (random UUID → Qdrant; 3,500-char truncation; no chunking/dedup/scoring).
5. **Config drift** — `qwen_client` semaphore=2 vs `max_qwen_workers`=1; `ollama` python lib pinned `0.2.1` (too old for JSON-schema structured output); `vc_qdrant` unhealthy.

---

## Target Architecture

**Principle: separate work by *volume*, not by capability.** The agent (brain) decides *what/when/why* to scout and commands the deterministic executor (muscle), which does the high-volume work on the small model. A GPU mutex guarantees the agent's 14B reasoning and the extraction loop never run at once.

```
   Schedule (cron-like)  ┌──────────────────────────────────────────────┐
   ──────────────────────▶            FastAPI (api/main.py)              │
                         │  + APScheduler jobs (staggered, no overlap)   │
                         │  + ScoutController  ◀── targeted-ingest cmd ──┼──┐
                         │    (GPU mutex / health guard / run history)   │  │
                         └───────────────┬───────────────────────────────┘  │
                                         │ runs one source at a time         │
                                         ▼                                   │
   Sources ─▶ crawl ─▶ chunk ─▶ candidate_filter ─▶ EXTRACT ─▶ dedup+score ─▶ PG + Qdrant
   (RSS, accelerators,                              (qwen2.5:7b,             │
    universities, Gmail)                             JSON schema)           │ commands
                                                                            │ (propose→act)
   ElizaOS "SCOUT" agent (Node, Discord) ───────────────────────────────────┘
     BRAIN: NL interface + multi-step gap-filling research loop
     (search → detect coverage gap → propose targeted ingest → confirm → re-search → synthesize)
     uses qwen3:14b on-demand, acquires the SAME mutex — never collides with extraction
```

**Locked decisions:** local right-sized models (A.2) · deterministic executor commanded by the agent (Phase 3/4) · ElizaOS-on-Node agent · agent autonomy = **propose-then-act** (autonomous on read/search/analysis; **confirms in Discord before heavy ingestion or writes**) · agent's flagship power = **gap-filling research loop** (other powers deferred).

---

## Phase 0 — Stabilize & introduce the model tier
**Effort: ~0.5 day. Goal:** fix live breakage and add the two-tier model split with *no behavior change yet*.

**Read first:** §A, §B, `config/__init__.py`, `reasoning/qwen_client.py`, `requirements.txt`, `docker-compose.yml`.

**Tasks**
1. **Fix `vc_qdrant` (unhealthy).** Inspect `docker compose logs qdrant` and the healthcheck in `docker-compose.yml`; recreate the container/volume as needed until `docker ps` shows it healthy. Confirm Qdrant answers on `:6333`.
2. **Bump the Ollama python lib.** In `requirements.txt`, raise `ollama==0.2.1` to a version supporting JSON-schema structured output (`>=0.4.7`). Reinstall. Smoke-test that `reasoning/qwen_client.py` still imports and `qwen_client.generate("ping")` works.
3. **Pull the extraction model:** `ollama pull qwen2.5:7b-instruct`.
4. **Add model-tier config** to `config/__init__.py::Settings`: `ollama_extract_model: str = "qwen2.5:7b-instruct"` (keep `ollama_reason_model = "qwen3:14b"`). Override via `.env`.
5. **Fix the semaphore/worker mismatch** in `reasoning/qwen_client.py` (currently a hard-coded `Semaphore(2)` while `max_qwen_workers=1`): derive the cap from `settings.max_qwen_workers`.
6. **Document required Ollama env** (in README or a new `RUNBOOK.md` stub): `OLLAMA_KEEP_ALIVE=5m`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`.

**DO NOT:** change extraction behavior or prompts yet (that's Phase 1); touch `config.py`; remove `qwen3:14b`.

**Verify / Done when:** both Docker containers healthy; `ollama list` shows `qwen2.5:7b-instruct`; app boots; `from config import settings; settings.ollama_extract_model` returns the new value; existing `scripts/run_ingestion.py rss` still runs without import errors.

---

## Phase 1 — Fast, hardened extraction on the small model
**Effort: ~1.5–2 days. Goal:** move per-chunk extraction to `qwen2.5:7b-instruct` with guaranteed-valid JSON, and make it timeout-proof.

**Read first:** §A, §B, `reasoning/qwen_client.py`, `reasoning/prompts.py`, `ingestion/worker_queue.py` (focus `_qwen_extract_sync`, `qwen_worker_task`), `ingestion/candidate_filter.py`, `ingestion/chunker.py`, `scripts/run_validation.py`.

**Tasks**
1. **Add an extraction method/path** in `reasoning/qwen_client.py` (e.g. `extract_startups(text) -> list[dict]`) that:
   - targets `settings.ollama_extract_model`,
   - passes a **JSON-array schema** via the Ollama `format=` parameter (structured output) so the model returns guaranteed-valid JSON,
   - **skips** the `<think>`-stripping and `parse_json_array` repair hacks (not needed with schema + non-thinking model),
   - keeps the `NEWSLETTER_EXTRACTION_PROMPT` content (incl. the strict exclusion rules) but adapts formatting for schema mode.
2. **Point the pipeline at it:** in `ingestion/worker_queue.py::_qwen_extract_sync`, call the new extraction method instead of `qwen_client.generate(...)`. Keep the existing `run_in_executor` dispatch and the `(startups, elapsed_s)` return shape used by validation.
3. **Timeout hardening:** replace the fixed 120s with a sensible per-call timeout for the 7B model **plus one bounded retry/backoff** so a transient stall doesn't lose a chunk.
4. **Cut LLM volume:** tighten `ingestion/candidate_filter.py` thresholds if precision allows, and (optional, time-permitting) add structural "card" parsing for portfolio/list pages in `ingestion/web_scraper.py` so obviously-repeating company blocks are captured without an LLM call.
5. **Per-source caps + checkpoint:** ensure a run respects max pages/chunks per source and can resume rather than dying mid-run.

**DO NOT:** route extraction through `qwen3:14b`; change `upsert_startup`/dedup/scoring; alter the validation record schema (`validation/capture.py`).

**Verify / Done when:** `python scripts/run_validation.py capture --source-url <an accelerator/HTGF page>` then `... export` / `... metrics` shows **0 timeouts**, all chunks processed, precision ≥ the prior `qwen3:14b` baseline, and median chunk latency roughly a third of before. Record the metrics.json in the PR description.

---

## Phase 2 — Gmail newsletter scouting (routed through the good pipeline)
**Effort: ~1.5–2 days. Goal:** newsletters flow through the *same* chunk→filter→extract→**upsert_startup** path, on a schedule, against the user's new Gmail account.

**Read first:** §A (esp. A.4), §B, `ingestion/newsletter_ingestor.py`, `processing/storage.py::upsert_startup`, `ingestion/chunker.py`, `ingestion/candidate_filter.py`, `config/source_registry.py`, `database/models.py` (`NewsletterEntry`), `api/main.py`, `api/routes/ingestion.py`.

**Tasks**
1. **Kill the bypass.** Rewrite `NewsletterIngestor._extract_startups` so each email body is **chunked** (`ingestion/chunker.py`) → **filtered** (`candidate_filter.is_relevant`) → extracted (Phase 1 method) → persisted via `processing/storage.upsert_startup()` with `source="newsletter"` + provenance (sender, subject, message id). Remove the `uuid4()` → `qdrant_store.upsert_startup` direct write and the `text[:3500]` truncation.
2. **Keep the audit trail:** still write the `NewsletterEntry` row (`_save_email_record`) for raw-email history; just don't use it as the startup store.
3. **Trusted-sender allowlist / newsletter registry:** extend `config/source_registry.py` (or a small config list) so only known VC/startup newsletters are processed; cross-email dedup is then automatic via fingerprint.
4. **Incremental fetch:** track the last-processed Gmail message id / `historyId` so each run only handles new mail (don't re-extract the same 50 every time).
5. **OAuth for the new account:** wire `credentials/gmail_credentials.json` → `token.json` flow (already implemented in `_authenticate`); document the first-run browser consent in the runbook.
6. **Schedule it:** add a Gmail APScheduler job in `api/main.py` (every 6–12h), **staggered** so it doesn't overlap RSS. (Until Phase 3 lands, just stagger times; Phase 3 adds the mutex.)

**DO NOT:** bypass `upsert_startup` (A.4); commit `credentials/` or tokens; change the extraction model (use Phase 1's).

**Verify / Done when:** subscribe the new Gmail to 2–3 newsletters; run ingestion; confirm extracted startups appear as PG `startups` rows **with scores** and a `source_history` entry citing the newsletter; confirm a startup seen on the web *and* in a newsletter resolves to **one** deduped row (same fingerprint UUID).

---

## Phase 3 — Deterministic executor + agent command surface
**Effort: ~1–1.5 days. Goal:** guarantee the Mac is never oversubscribed and give the agent a clean lever to drive ingestion.

**Read first:** §A (esp. A.3), §B, `api/main.py`, `api/routes/ingestion.py`, `ingestion/pipeline.py`, `ingestion/worker_queue.py`.

**Tasks**
1. **Create `processing/scout_controller.py`** (or a new `orchestration/` module) exposing a singleton controller that:
   - owns the **GPU mutex** (`asyncio.Lock`); every ingestion run and every 14B reasoning call acquires it,
   - runs sources **sequentially**,
   - does **pre-flight health checks** (Ollama reachable? Qdrant healthy?) that **skip + log** instead of crashing,
   - records a **run history** (start/end, source, counts, errors) queryable via API.
2. **Route all ingestion through it:** refactor the APScheduler jobs and `/ingestion/run-all` in `api/main.py` / `api/routes/ingestion.py` to call the controller; **stagger** RSS / newsletters / accelerators / universities so heavy jobs never overlap.
3. **Add `POST /ingestion/targeted`** (the agent's lever): accepts a specific source id / URL / focused query, runs *just that* through the controller under the mutex, returns a `run_id`.
4. **Add `GET /ingestion/status`:** current run, last run, queue depths, recent run history — so a caller can poll until a targeted run completes.

**DO NOT:** let any path call Ollama for ingestion without going through the controller's mutex; put scouting *decisions* in the controller (it's mechanical muscle — decisions live in the agent, Phase 4).

**Verify / Done when:** triggering `run-all` while a scheduled job is due → the second waits on the mutex (no overlap in logs); `POST /ingestion/targeted` runs one source and reports completion via `/ingestion/status`; killing Ollama mid-run → clean skip/retry, no crash; `ollama ps` never shows two heavy models loaded at once.

---

## Phase 4 — OpenClaw agent: the gap-filling research brain (ElizaOS on Node)
**Effort: ~3 days (highest uncertainty — kept last). Goal:** stand up SCOUT as the decision layer that *dictates the process* via a multi-step research loop, bounded by propose-then-act and the GPU mutex.

**Read first:** §A (esp. A.2, A.3, locked decisions), §B, `openclaw/character.json`, `openclaw/tools_config.json`, `api/routes/*` (the tool endpoints), Phase 3's `/ingestion/targeted` + `/ingestion/status`.

**Tasks**
1. **Scaffold an ElizaOS project** (Node 22 is installed) that loads the existing `openclaw/character.json` and maps `openclaw/tools_config.json` tools to the live FastAPI endpoints. Provide the Discord bot token via env (never commit it).
2. **Add the tools the loop needs:** `ingest_targeted` → `POST /ingestion/targeted`; `ingestion_status` → `GET /ingestion/status`; `ingest_newsletters` → `POST /ingestion/newsletters`. Keep the existing 6 tools.
3. **Implement the gap-filling research loop** as SCOUT's core behavior:
   1. `search_startups` for the user's intent.
   2. Judge coverage (too few / stale / missing a sub-sector or geo).
   3. If a gap is found, **propose** a targeted scan in Discord ("thin coverage of climate-tech seed in Munich — scan HTGF + EU-Startups?") and **wait for confirmation**.
   4. On "yes" → `ingest_targeted` → poll `ingestion_status` until done → re-`search_startups` → synthesize an investor-grade answer.
4. **Model wiring:** small model for tool-routing/intent; **`qwen3:14b` for judgment + final synthesis**; every 14B call **acquires the Phase 3 GPU mutex** so reasoning never fights the extraction loop.
5. **Autonomy = propose-then-act:** read/search/analysis run without confirmation; **heavy ingestion and any DB writes require an explicit Discord confirmation.**

**DO NOT:** let the agent run the high-volume extraction loop itself or bypass the controller/mutex; auto-trigger ingestion without confirmation; load a second 14B instance; commit the Discord token. (Judgment-layer / daily-digest / validation-triage powers are **out of scope** — leave seams, don't build them.)

**Verify / Done when:** in Discord, a thin-coverage query runs the full loop — propose → confirm → targeted ingest → re-search → improved DB-backed answer; the same flow while a scheduled job runs **queues on the mutex** (no timeout/crash); `ollama ps` shows no concurrent second heavy model; RAM stays within budget.

---

## Phase 5 — End-to-end testing, metrics & runbook
**Effort: ~1 day. Goal:** prove it holds up under real load and document operation.

**Read first:** §A, §B, `scripts/run_validation.py`, `RUNBOOK.md` (created/expanded here).

**Tasks**
1. **Full E2E run** (RSS + accelerators + universities + Gmail) within a fixed memory/time budget; capture validation precision/recall via the harness.
2. **Failure injection:** kill Ollama mid-run; stop Qdrant; expire the Gmail token — confirm graceful degradation (skip/log/clear error), no crash.
3. **Light load test:** drive the agent while a scheduled ingestion is queued; confirm mutex serialization and bounded RAM.
4. **Write `RUNBOOK.md`:** start/stop, required Ollama env (A.1/A.2), OAuth re-auth, model swap, "what to do when X fails," and how to read run history + validation metrics.

**Verify / Done when:** a documented green E2E run with **0 timeouts**, recorded peak RAM well under 24 GB, and all three failure-injection cases handled gracefully.

---

## Timeline

| Phase | Scope | Est. |
|---|---|---|
| 0 | Stabilize + model tier | ~0.5 day |
| 1 | Fast/hardened extraction | ~1.5–2 days |
| 2 | Gmail newsletter scouting | ~1.5–2 days |
| 3 | Deterministic executor + command surface | ~1–1.5 days |
| 4 | ElizaOS agent — gap-filling brain | ~3 days |
| 5 | E2E testing + runbook | ~1 day |
| **Total** | | **~9–11 working days** (≈ 2–2.5 calendar weeks with testing) |

Reliability core (Phases 0–3) ≈ **4.5–6 days** — after that, ingestion + Gmail are solid even before the agent ships. Phase 4 is highest-uncertainty and intentionally last.

---

## Risks & mitigations
- **Small-model extraction quality drop** → JSON-schema structured output + the validation harness gate each change; `qwen3:14b` stays available as a per-source override if a specific source needs it.
- **ElizaOS integration friction (Phase 4)** → isolated last; Phases 0–3 deliver a fully working headless system; Discord slash commands (`discord_bot/bot.py`) remain a fallback interface.
- **Gmail "Testing" OAuth tokens expire ~7 days** → runbook documents re-auth; consider moving the Google Cloud app to "Production" for long-lived refresh tokens.
- **Memory pressure if both 7B + 14B pin** → `OLLAMA_KEEP_ALIVE` + `OLLAMA_MAX_LOADED_MODELS=2`; the mutex ensures only one heavy job is active, so the idle model can unload.
