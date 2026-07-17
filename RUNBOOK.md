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

**On the Mac mini, this is now automatic** — see "Unattended operation" below. The steps here are for manual/dev use or debugging.

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
Edit `config/sources.yaml` → `newsletter_senders` (no restart needed — re-read fresh on every run; see "Dynamic sources" below):
```yaml
newsletter_senders:
  - sifted.eu
  - eu-startups.com
  - dealroom.co
```
Leave the list empty (the default) to process **all** emails matching the search terms — relevance is still filtered by content downstream, which is the current setup since greentechhubx@gmail.com is a dedicated scouting inbox.

### Incremental fetch state
Processed message IDs are tracked in `credentials/newsletter_state.json`. This prevents re-processing the same 50 emails on every scheduler tick. The file is auto-created on the first run. Delete it to force a full re-scan.

### Schedule
- **Full sweep** (RSS + accelerators + universities + newsletters): **Monday and Thursday at 05:00**.
- **Gmail top-up** (incremental, cheap): **daily at 13:00**, so newsletters arriving between sweeps don't wait days.

To trigger a manual run any time:
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

## Dynamic sources

All RSS feeds, web sources, newsletter senders, and Gmail search terms live in `config/sources.yaml` — not in Python code. It's re-read fresh on every ingestion run, so edits take effect on the next run with no restart, no deploy.

- Edit the file directly, or use the API:
  ```bash
  curl http://localhost:8000/sources                    # list everything
  curl -X POST http://localhost:8000/sources/web -d '{...}'   # add a web source
  curl -X POST http://localhost:8000/sources/rss -d '{...}'   # add an RSS feed
  curl -X DELETE http://localhost:8000/sources/web/<source_id>
  ```
- A malformed entry is skipped and logged — it never crashes a run. A totally broken file falls back to the last version that worked.
- New entries added via the API/dashboard get an auto "Added via dashboard on \<date\>" comment; human-written comments in the file always survive edits.

## Unattended operation (Mac mini, runs for weeks with nobody there)

The system is designed to survive reboots, crashes, and long absences with zero manual intervention. Two things make this work, both installed as `launchd` agents (templates version-controlled in `launchd/` — reinstall with the commands below if the machine is ever rebuilt):

| Agent | What it does |
|---|---|
| `com.vcscouting.dockerstack` | Runs once at every login: waits (up to 3 min) for the Docker daemon to be ready, then `docker compose up -d`. **This exists because Docker Desktop's own container restart-on-reboot was found to be unreliable** in a real reboot test — containers were left in an "Exited (255)" state after a full macOS restart and did not resume on their own even with `restart: unless-stopped`. Logs to `logs/docker_stack.log`. |
| `com.vcscouting.api` | Runs the FastAPI server + scheduler **and serves the team dashboard** (see below) from the same process. `RunAtLoad` + `KeepAlive` — if it ever crashes (e.g. because Postgres wasn't ready yet at boot), it retries every 10s until it succeeds. Logs to `logs/api.log` (uvicorn access logs) and `logs/api.error.log` (application logs — Python's `logging` module writes to stderr by default). |
| Ollama.app | Already auto-starts at login as a standard macOS app — no custom agent needed. |

There used to be a third agent (`com.vcscouting.dashboard`, a Streamlit app on port 8501) — it's retired. The dashboard is now a static HTML/CSS/JS app (`ui/static/`) served by the API itself via FastAPI's `StaticFiles`, so it needs no separate service, no separate port, and no separate deploy step: it's live the moment `com.vcscouting.api` is up.

## Team dashboard (Phase UI)

Custom-built (no Streamlit, no build step — plain HTML/CSS/JS so there's nothing to compile and nothing to break unattended) branded dashboard with 5 pages: **Overview** (KPIs, charts, activity), **Browse & Search** (keyword + semantic search, filters, edit, delete, CSV export), **Review Inbox** (the data-stewardship staging queue — see below), **Ingestion** (trigger any job, watch it run live with ticking counters), **Sources** (add/remove RSS feeds and web sources — no code, no YAML editing).

- **URL:** `http://<mac-mini-LAN-IP>:8000/dashboard` (find the IP with `ipconfig getifaddr en0`; `http://<IP>:8000/` also redirects there). Office Wi-Fi only — stays fully local, same-origin to the API (no CORS, no separate base-URL config).
- **⌘K** opens a command palette to jump to any page or run a quick action (full sweep, theme toggle) without the mouse.
- **Light/dark theme**, persisted per-browser, defaults to the OS preference.
- **Review Inbox:** the pipeline never changes existing startup data on its own. Every field change and every possible-duplicate is **staged** for a human. Markers: 🔴 conflict (a populated field would change) · 🟡 new info (fills a blank) · ⚠️ anomaly (e.g. a shared domain like linkedin.com with nothing else matching). *Approve* applies the change to the master (or merges a duplicate); *Reject* discards it **and remembers the decision** so the same thing isn't re-flagged on the next sweep. Keyboard shortcuts: `j`/`k` navigate, `a` approve, `r` reject. A nightly job (02:00) has the local 14B model write a plain-language explanation of the evidence for each item — guidance only, never a decision.
- **Ingestion page:** trigger a full sweep, RSS, newsletters, accelerators, universities, or one specific source. While a run is active, counters (pages crawled, chunks, startups found) tick live — polled every 2s from `/ingestion/status`, which now exposes the in-flight `PipelineMetrics` object, not just the final result.
- **Sources page:** the "Add source" form writes straight into `config/sources.yaml` via the existing `/sources` API — the next scheduled run (or a manual "Run now" from the same page) picks it up with no restart.
- **Development:** no build step — edit files under `ui/static/` and reload the browser. `ui/static/js/api.js` calls the API same-origin (empty base URL) since it's always served by the same FastAPI process — there's no separate host to configure.

Also required for full unattended survival (system settings, not code):
- **No sleep**: `sudo pmset -c sleep 0 displaysleep 0 disksleep 0`
- **Docker Desktop → Settings → General → "Start Docker Desktop when you log in"**
- **Automatic login** (System Settings → Users & Groups → Login Options) — without this, a `launchd` **Agent** (as opposed to a Daemon) never runs at all, since it only starts within a logged-in GUI session. A reboot with no auto-login sits at the lock screen forever.

### Reinstalling the launchd agents (e.g. after a fresh macOS install)
```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vcscouting.dockerstack.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vcscouting.api.plist
```

### Verifying it's actually working
```bash
launchctl list | grep vcscouting     # both should show a PID, not "-"
docker ps                            # vc_postgres and vc_qdrant both (healthy)
curl http://localhost:8000/health
```

### Full reboot rehearsal (do this before any long absence)
```bash
sudo shutdown -r now
```
Wait for the Mac to come back, log in with nobody touching a terminal, then run the verification commands above. This exact test caught the Docker container issue described above — don't skip it.

## Automated tests

An integration test suite lives in `tests/` (pytest). It runs against the **live** Postgres + Qdrant + Ollama so it exercises the real wiring, not mocks. All test data is namespaced with a `PYTEST` prefix and purged before and after every test — the real startups are never touched.

```bash
# from the project root, with Docker + Ollama running:
python3 -m pytest            # run everything (~5s, ~34 tests)
python3 -m pytest -v         # verbose, one line per test
python3 -m pytest tests/test_storage_staging.py   # one module
python3 -m pytest -k dedup   # tests matching a keyword
```

Coverage: identity functions (`test_deduplicator`), the matcher's classification incl. the shared-domain blocklist (`test_matcher`), the staging outcomes new/no_op/staged_update/duplicate/anomaly (`test_storage_staging`), review approve/reject + suppression (`test_reviews`), search/filter/edit/delete (`test_scout_api`), the dynamic source registry (`test_sources`), the tuning loader + hot-reload + safe-fallback (`test_tuning`), and the scorer tiers (`test_scorer`).

**Run the suite after any change to matching, storage, scoring, or the config loaders.** Requires the services up (`docker ps`, `ollama` reachable); if Ollama is down the embedding-dependent tests will error rather than fail silently.

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `vc_qdrant` unhealthy | `docker inspect vc_qdrant` | Healthcheck uses bash `/dev/tcp`; if still failing restart: `docker compose restart qdrant` |
| Containers "Exited" after a reboot, don't come back | `docker ps -a` | Confirm `com.vcscouting.dockerstack` ran: `cat logs/docker_stack.log`. Manually recover with `docker compose up -d`. |
| API not responding after a reboot | `launchctl list \| grep vcscouting` | If PID is `-`, check `logs/api.error.log` — usually means Postgres/Qdrant weren't ready; it retries automatically every 10s once they are. |
| Ollama timeouts | `ollama ps` | Only one model should be loaded for extraction; GPU oversubscription means 2 are fighting. Set `OLLAMA_NUM_PARALLEL=1`. |
| Extraction returns empty arrays | Run validation harness | Check `qwen2.5:7b-instruct` is pulled and `OLLAMA_EXTRACT_MODEL` points to it |
| Gmail ingestion finds 0 emails | Check search terms | `newsletter_search_terms` in `config/sources.yaml` controls the subject-line filter (`newer_than:14d` is fixed) |
| PG connection refused | `docker ps` | `docker compose up -d postgres` |

## Configuration

All settings are in `config/__init__.py` (pydantic-settings). Override any via `.env`:

```bash
OLLAMA_EXTRACT_MODEL=qwen2.5:7b-instruct   # extraction model
OLLAMA_REASON_MODEL=qwen3:14b              # reasoning / agent model
MAX_QWEN_WORKERS=1                         # keep at 1 on Mac Mini
GMAIL_CREDENTIALS_PATH=./credentials/gmail_credentials.json
```
