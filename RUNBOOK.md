# VC Scouting System — Operations Runbook

## Required Ollama environment

Set these before starting the API server or any ingestion script. Add them to your shell profile or `.env`:

```bash
export OLLAMA_KEEP_ALIVE=5m          # Unload idle models after 5 min (frees memory)
export OLLAMA_MAX_LOADED_MODELS=2    # Allow the 7B extract + 14B reason models in memory at once
export OLLAMA_NUM_PARALLEL=1         # One request at a time per model (prevents GPU oversubscription)
```

## Models

| Purpose | Model | Pull command |
|---|---|---|
| **Extraction (hot path)** | `qwen2.5:7b-instruct` | `ollama pull qwen2.5:7b-instruct` |
| **Reasoning / agent** | `qwen3:14b` | (already installed) |
| **Embeddings** | `nomic-embed-text` | (already installed) |

All inference is **local only** — never configure a cloud provider.

## Starting the system

```bash
# 1. Start infrastructure
docker compose up -d

# 2. Verify containers
docker ps   # vc_postgres and vc_qdrant should both be (healthy)

# 3. Set Ollama env (see above)

# 4. Start the API server
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## Manual ingestion

```bash
# RSS feeds only (fast, ~1 min)
python scripts/run_ingestion.py rss

# All accelerator portfolio pages
python scripts/run_ingestion.py accelerators

# University spinoff pages
python scripts/run_ingestion.py universities

# Full run (all sources)
python scripts/run_ingestion.py all
```

## Ingestion controller & API (Phase 3)

All server-side ingestion runs through `processing/scout_controller.py`, which
holds a single **GPU mutex** (`asyncio.Lock`). Only one heavy LLM job touches
Ollama at a time — scheduled jobs, API-triggered runs, and (Phase 4) the agent's
14B reasoning all queue on the same lock, so the Mac is never oversubscribed.

Before any run the controller pre-flights Ollama + Qdrant; if either is down the
run is **skipped and logged**, never crashed. Every run is tracked in an
in-memory history (last 50) queryable via the status endpoint.

```bash
# Trigger ingestion (all queue on the GPU mutex, return immediately)
curl -X POST http://localhost:8000/ingestion/rss
curl -X POST http://localhost:8000/ingestion/scrape-accelerators
curl -X POST http://localhost:8000/ingestion/scrape-universities
curl -X POST http://localhost:8000/ingestion/newsletters
curl -X POST http://localhost:8000/ingestion/run-all      # RSS → accel → uni → newsletters

# Targeted run (the agent's lever) — returns a run_id to poll
curl -X POST http://localhost:8000/ingestion/targeted \
     -H 'Content-Type: application/json' \
     -d '{"source_id":"munich_startup"}'        # or {"kind":"rss"} / {"url":"https://..."}

# Controller status: current run, last run, recent history, GPU lock state
curl http://localhost:8000/ingestion/status

# Poll a specific targeted run until status == completed | failed | skipped
curl "http://localhost:8000/ingestion/status?run_id=<run_id>"
```

Scheduled jobs (set in `api/main.py`): RSS every 6 h, Gmail every 8 h (offset
+30 min). Both call the controller, so they serialize against each other and
against any API-triggered run.

> The manual CLI (`scripts/run_ingestion.py`) calls the scraper directly and is
> intended for use when the API server is **not** running. Do not run it
> alongside the server — the in-process mutex cannot guard a separate process.

## Validation harness

```bash
# Capture an extraction run for quality review
python scripts/run_validation.py capture --source-url https://www.htgf.de/en/portfolio/

# Export to CSV for manual review
python scripts/run_validation.py export --run-id <UUID from capture>

# Compute precision/recall metrics (after filling verdict column in CSV)
python scripts/run_validation.py metrics --run-id <UUID>
```

Validation outputs land in `validation/{run_id}/`.

## Gmail newsletter setup

The dedicated scouting Gmail account is **greentechhubx@gmail.com**. Newsletters already subscribed there are automatically processed on the 8-hour schedule.

### Subscribing to new newsletters
Subscribe greentechhubx@gmail.com to VC/startup newsletters (e.g. Sifted, EU-Startups, Dealroom, TechCrunch, etc.). The ingestor will extract startups from each email using the same pipeline as web sources.

### Trusted-sender allowlist
Edit `ingestion/newsletter_ingestor.py` → `TRUSTED_NEWSLETTER_SENDERS`. Add the domain or substring from the sender's `From` header:
```python
TRUSTED_NEWSLETTER_SENDERS = [
    "sifted.eu",
    "eu-startups.com",
    "dealroom.co",
]
```
Leave the list empty to process **all** emails matching the search query (useful during setup).

### Incremental fetch state
Processed message IDs are tracked in `credentials/newsletter_state.json`. This prevents re-processing the same 50 emails on every scheduler tick. The file is auto-created on the first run. Delete it to force a full re-scan.

### Schedule
Gmail ingestion runs **every 8 hours**, starting 30 minutes after the API server starts. This staggers it away from the RSS job (every 6 hours). To trigger a manual run:
```bash
curl -X POST http://localhost:8000/ingestion/newsletters
```

### OAuth re-authentication
The token at `credentials/token.json` expires every 7 days for Google Cloud apps in "Testing" mode.

To re-authenticate:
```bash
rm credentials/token.json
# Then trigger newsletter ingestion — a browser window will open for consent:
curl -X POST http://localhost:8000/ingestion/newsletters
```

To avoid the 7-day expiry, promote the Google Cloud app from "Testing" to "Production" in the Google Cloud Console.

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `vc_qdrant` unhealthy | `docker inspect vc_qdrant` | Healthcheck uses bash `/dev/tcp`; if still failing restart: `docker compose restart qdrant` |
| Ollama timeouts | `ollama ps` | Only one model should be loaded for extraction; GPU oversubscription means 2 are fighting. Set `OLLAMA_NUM_PARALLEL=1`. |
| Extraction returns empty arrays | Run validation harness | Check `qwen2.5:7b-instruct` is pulled and `OLLAMA_EXTRACT_MODEL` points to it |
| Gmail ingestion finds 0 emails | Check query filter | `NEWSLETTER_SEARCH_QUERY` in `ingestion/newsletter_ingestor.py` filters by subject keywords and `newer_than:14d` |
| PG connection refused | `docker ps` | `docker compose up -d postgres` |

## Configuration

All settings are in `config/__init__.py` (pydantic-settings). Override any via `.env`:

```bash
OLLAMA_EXTRACT_MODEL=qwen2.5:7b-instruct   # extraction model
OLLAMA_REASON_MODEL=qwen3:14b              # reasoning / agent model
MAX_QWEN_WORKERS=1                         # keep at 1 on Mac Mini
GMAIL_CREDENTIALS_PATH=./credentials/gmail_credentials.json
```
