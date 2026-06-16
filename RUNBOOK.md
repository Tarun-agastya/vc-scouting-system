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

## Gmail OAuth re-authentication

The new Gmail account uses OAuth2. The token at `credentials/token.json` expires every 7 days for Google Cloud apps in "Testing" mode.

To re-authenticate:
```bash
rm credentials/token.json
# Then trigger newsletter ingestion — a browser window will open for consent:
python scripts/run_ingestion.py  # or POST /ingestion/newsletters
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
