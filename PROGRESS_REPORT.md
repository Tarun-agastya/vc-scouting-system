# GreenTech Hub — VC Scouting System
## Progress Report · June 2026

---

## What We Built

An AI-powered startup intelligence platform that automatically discovers, extracts, deduplicates, and scores early-stage technology startups across the **DACH region and Europe**. It monitors accelerator portfolio pages, university incubator sites, RSS feeds, and incoming newsletters from sources like SCE, Munich Startup, Unternehmertum, Startpicker, Digitales Zentrum Schwaben, and KIT Gründerschmiede — all running **100% locally** on a Mac Mini (no cloud LLMs, no data leaving our infrastructure).

---

## System Architecture

```
Web Sources (accelerators, universities, VC portfolios)
Gmail (greentechhubx@gmail.com — VC newsletters)
RSS Feeds
        │
        ▼
   Crawler + Chunker
        │
        ▼
   AI Extraction (local Qwen 7B model, JSON structured output)
        │
        ▼
   Deduplication (fingerprint + fuzzy matching)
        │
        ▼
   Scoring (0–100 enrichment score, source confidence)
        │
        ├──▶ PostgreSQL (structured startup profiles)
        └──▶ Qdrant (vector DB for semantic search)
                │
                ▼
         SCOUT Agent (Discord chat interface — coming in Phase 4)
```

**Privacy / Security:** All LLM inference runs on-device. No startup data is sent to OpenAI, Anthropic, or any external API.

---

## Validation Test — HTGF Portfolio Page

We ran three consecutive tests against the same source (High-Tech Gründerfonds portfolio, one of Germany's most active pre-seed investors) to measure improvement at each stage of development.

### Results at a Glance

| Metric | Baseline (old system) | After model fix | Current (Phase 1) | Change |
|---|---|---|---|---|
| **Model used** | qwen3:14b | qwen3:14b (thinking) | qwen2.5:7b-instruct | Smaller, faster, specialized |
| **Pages crawled** | 5 | 1 | 5 | Full BFS crawl restored |
| **Chunks processed** | 14 | 11 | 10 | Smarter pre-filter |
| **Startups extracted** | 0 | 8 | 16 unique | +100% vs. intermediate |
| **Stored to database** | 0 | 8 | 31 raw → 16 after dedup | Dedup working correctly |
| **Extraction rate** | 0% | 72.7% | ~85% | Significant improvement |
| **Avg time per chunk** | 9.9s (but failing) | 86.7s | 34.9s | **2.5× faster** |
| **Timeouts** | Multiple | 2 of 11 chunks | **0** | Fully resolved |
| **Duplicates caught** | 0 (counter broken) | 0 (counter broken) | **15 merges tracked** | Bug fixed |

### What the Numbers Mean

**Baseline problem:** The system was using a *thinking* model (`qwen3:14b`) designed for complex reasoning tasks — not bulk data extraction. It spent 40–50 seconds per chunk generating internal reasoning (`<think>` blocks) that were immediately discarded. Result: frequent timeouts, 0 startups extracted from 14 chunks, system unusable.

**Phase 1 fix:** Switched the extraction hot-path to `qwen2.5:7b-instruct` with JSON schema constraints (Ollama structured output). The model now returns guaranteed-valid JSON without thinking steps. Time per chunk dropped from 86.7s to 34.9s average, timeouts eliminated, and extraction quality improved because the output format is deterministic.

---

## Startups Found — HTGF Portfolio (Current Run)

16 unique tech startups extracted and stored with full profiles:

| Company | Sector | Status |
|---|---|---|
| EGYM | FitTech / Digital Health | Stored ✓ |
| Proxima Fusion | Energy / Deeptech | Stored ✓ |
| SimScale | Engineering SaaS | Stored ✓ |
| VMRay | Cybersecurity | Stored ✓ |
| Instagrid | Energy Storage Hardware | Stored ✓ |
| Tubulis | Biotech / ADC | Stored ✓ |
| Argá Medtech | MedTech | Stored ✓ |
| EEDEN | CleanTech / Textiles | Stored ✓ |
| doinstruct | EdTech / HR | Stored ✓ |
| Plancraft | PropTech / ConTech | Stored ✓ |
| Certivity | RegTech / LegalTech | Stored ✓ |
| Avelios Medical | Digital Health | Stored ✓ |
| ATMOS Space Cargo | Space Tech | Stored ✓ |
| Biograil | BioTech / Rail | Stored ✓ |
| Next Kraftwerke | Energy / Grid | Stored ✓ |
| Avelios | Digital Health | Merged with Avelios Medical ✓ |

> **Deduplication in action:** Companies like Avelios / Avelios Medical appearing on multiple pages of the same site are automatically merged into a single database record using fingerprint matching (SHA-256 of normalized name + domain). 15 duplicate extraction events were resolved to 16 unique records.

---

## Current Database State

- **36 startup profiles** stored across all sources
- Each profile includes: name, description, website, industry, city/country, funding stage, founded year, tags, source history, enrichment score
- Each startup scored 0–100 with a tier label (STRONG_SIGNAL / GOOD_SIGNAL / WEAK_SIGNAL) based on data completeness and source confidence
- Full deduplication: the same startup seen on an accelerator page and in a newsletter resolves to **one record**

---

## Key Engineering Improvements (Phases 0–3)

### Phase 0 — Infrastructure Stabilization
- Fixed broken Qdrant container (healthcheck command not available in v1.18.0)
- Upgraded Ollama Python library to support JSON schema structured output
- Fixed semaphore/worker mismatch causing concurrency bugs

### Phase 1 — Fast, Reliable Extraction
- Replaced `qwen3:14b` (thinking model, 87s/chunk) with `qwen2.5:7b-instruct` (35s/chunk) for extraction
- Added Ollama structured output with JSON schema — no more parse failures or invalid JSON
- Rewrote extraction prompt to correctly include unicorns, scale-ups, and growth-stage companies
- Result: **0 timeouts, 2.5× faster, higher extraction quality**

### Phase 2 — Gmail Newsletter Integration
- Connected `greentechhubx@gmail.com` to the pipeline (OAuth2)
- Newsletters from SCE, Munich Startup, Unternehmertum, Startpicker, Digitales Zentrum Schwaben, KIT Gründerschmiede are now automatically processed
- Newsletter-sourced startups go through the same dedup/scoring pipeline as web sources — a startup seen in a newsletter and on an accelerator site resolves to one record
- Incremental fetch: only new emails are processed each run (no re-processing)
- Gmail ingestion scheduled every 8 hours (offset from web crawl jobs)

### Phase 3 — Deterministic Executor (GPU Mutex)
- Built `ScoutController`: a single asyncio lock that serializes **all** heavy LLM work
- Prevents the Mac Mini from being oversubscribed — extraction and agent reasoning can never run simultaneously
- Pre-flight health checks on Ollama + Qdrant before every run (skip gracefully if down, never crash)
- New API endpoints:
  - `POST /ingestion/targeted` — trigger a focused ingestion for one source and get a `run_id`
  - `GET /ingestion/status` — real-time view of current run, last run, full history with metrics
- All scheduled jobs (RSS every 6h, Gmail every 8h) now route through the controller

---

## What's Next — Phase 4

**SCOUT Discord Agent** — the decision-making layer that turns this into an active intelligence tool rather than a passive database.

The agent will run a **gap-filling research loop**:
1. User asks in Discord: *"Show me seed-stage climate tech in Munich"*
2. SCOUT searches the database and spots thin coverage
3. SCOUT proposes: *"I found 3 results but coverage is thin — should I scan HTGF + Campus Founders for more?"*
4. User confirms → targeted ingestion runs → SCOUT re-searches → delivers an investor-grade answer

**Key design decisions already locked in:**
- Uses `qwen3:14b` locally for reasoning and synthesis (no cloud)
- Acquires the same GPU mutex as ingestion (so reasoning and extraction never collide)
- **Propose-then-act autonomy**: SCOUT runs searches and analysis freely; it asks for confirmation before triggering ingestion or writing to the database

**Prerequisite to start Phase 4:** Discord bot token (create a bot at discord.com/developers, invite it to your server).

---

## Privacy & Security Posture

| Item | Status |
|---|---|
| LLM inference | 100% local (Ollama on Mac Mini) |
| Startup data | PostgreSQL + Qdrant, local Docker containers |
| Gmail OAuth | Credentials in gitignored `credentials/` folder, never committed |
| Discord bot token | Environment variable only, never in code |
| Cloud dependencies | None for inference; Gmail API (read-only) for newsletter fetch |

---

*Report generated: June 2026 | System: VC Scouting Intelligence Platform v1.0 | Branch: phase/0-stabilize-model-tier*
