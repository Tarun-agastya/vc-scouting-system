# VC Scouting System — Operations Runbook

**Last verified working: 13 Aug 2026.** Machine: Mac mini (M4, 24 GB), `~/vc-scouting-system/vc-scouting-system`.

This is the single source of truth for running, checking, and repairing the system. It is written to be usable by someone who did not build it.

---

## 1. The 60-second health check

Run these three. If all three look right, the system is fine — everything else in this document is for when they don't.

```bash
cd ~/vc-scouting-system/vc-scouting-system

curl -s http://localhost:8000/health          # → {"status":"ok","startups_in_db":2144}
docker ps --format "table {{.Names}}\t{{.Status}}"   # → 3 containers, all (healthy)
launchctl list | grep -E "vcscouting|gthub"   # → com.vcscouting.api has a real PID
```

**What "right" looks like:**

| Check | Healthy output | Meaning |
|---|---|---|
| `/health` | `{"status":"ok","startups_in_db":<number>}` | API + Qdrant both alive. The number only ever grows. |
| `docker ps` | `vc_postgres`, `vc_qdrant`, `vc_searxng` — all `(healthy)` | Databases + search fallback up. |
| `launchctl list` | `com.vcscouting.api` shows a **number** in column 1 | API service running. `-` means dead. |

For `com.vcscouting.dockerstack` and `com.gthub.pressmonitor`, a `-` in column 1 is **normal** — they are one-shot jobs, not services. They only hold a PID while actually running. Column 2 is their last exit code; `0` = last run succeeded.

---

## 2. What runs by itself

Nothing here needs a human. This is the full unattended schedule.

| When | Job | What it does | Where it's defined |
|---|---|---|---|
| **Mon + Thu 05:00** | Full sweep | RSS → accelerators → universities → newsletters. The main data intake. | `api/main.py` scheduler |
| **Daily 08:00** | Press monitor | Downloads the Memminger Zeitung e-paper, scans for watched keywords, emails a digest. | `com.gthub.pressmonitor` (separate launchd job) |
| **Daily 13:00** | Gmail top-up | Incremental newsletter check so mail arriving between sweeps isn't delayed. | `api/main.py` scheduler |
| **Nightly 02:00** | LLM explain | Local 14B model writes a plain-language explanation for pending reviews. Guidance only — never decides. | `api/main.py` scheduler |
| **Nightly 03:00** | Verification recheck | Re-verifies up to 80 unverified records against their own stored source text. Local only, no web calls. | `api/main.py` scheduler |
| **Nightly 04:00** | Web verify | Enriches up to 20 name-only stubs using a web search. **The only job that leaves the machine.** | `api/main.py` scheduler |
| **Nightly 04:30** | Reclassify | Safety net for records whose inline classification failed. | `api/main.py` scheduler |

All of the API-scheduler jobs queue on a single **GPU mutex**, so they never fight each other for Ollama. The press monitor is a separate process and shares only Ollama and the Gmail credential.

**Over a month with nobody watching, expect:** ~8 full sweeps, ~30 press digests, ~30 Gmail top-ups, and the nightly maintenance jobs every night. The startup count grows; the review queue grows (see §5).

---

## 3. How you'd know something broke — and the honest gap

**There is no alerting.** Nothing emails you if the API dies. Be aware of this before relying on silence as good news.

The closest thing to a daily heartbeat is the **press-monitor digest email** (~08:05 daily). But it is an imperfect signal:

> ⚠️ **The digest is only sent on days when keywords actually match.** A no-match day sends nothing at all. So a missing digest means *either* "nothing in the paper today" *or* "the system is broken" — you cannot tell which from your inbox.

If you want certainty while away, run the §1 health check remotely, or ask someone in the office to. Two or three missed digest days in a row is worth checking.

---

## 4. Troubleshooting by symptom

Ordered by how likely you are to hit it. Every fix here is safe to run.

### 4.1 API not responding / dashboard won't load

```bash
launchctl list | grep vcscouting          # is it running?
tail -50 logs/api.error.log               # why did it stop?
launchctl kickstart -k gui/$(id -u)/com.vcscouting.api   # restart it
```

The service has `KeepAlive`, so it restarts itself every 10s on crash. If it's flapping, it's almost always because Postgres or Qdrant isn't up yet — fix §4.2 first, and the API recovers on its own.

### 4.2 Containers down / "connection refused" to Postgres or Qdrant

```bash
docker ps -a                              # look for "Exited"
docker compose up -d                      # bring everything back
docker compose restart qdrant             # or just one
cat logs/docker_stack.log                 # did the login-time job run?
```

Docker Desktop's own restart-on-reboot proved unreliable in a real reboot test, which is why `com.vcscouting.dockerstack` exists — it waits for the Docker daemon at login and runs `docker compose up -d`. If containers are down after a reboot, that job is the first thing to check.

### 4.3 Press monitor digest stopped arriving

Two independent credentials can break it. Check the log first — it names which:

```bash
tail -40 logs/pressmonitor.log            # per-day outcome, one line each
tail -60 logs/pressmonitor.error.log      # the actual error
python3 -m press_monitor.run_daily        # run it by hand, right now
```

| Log says | Cause | Fix |
|---|---|---|
| e-paper login failed | Subscription login changed/lapsed | Update `EPAPER_EMAIL` / `EPAPER_PASSWORD` in `.env` |
| SMTP / auth error | Gmail App Password revoked | Regenerate at `myaccount.google.com/apppasswords`, update `GMAIL_APP_PASSWORD` in `.env` |
| `not_published` | Sunday / holiday — no edition exists | Nothing to fix |
| `no_matches` | Paper had no watched keywords | Nothing to fix |
| `MuPDF error: cannot find XObject` | Known, harmless — see §7 | Nothing to fix |

Running it by hand **sends a real email to the real recipients**. That's usually what you want when testing, but don't run it repeatedly.

### 4.4 Gmail newsletter ingestion failing

Authentication is an **App Password over IMAP/SMTP** — not OAuth. There is no token to expire, no browser consent, no Testing/Production mode. It works until the password is manually revoked or 2-Step Verification is turned off on the account.

Check in this order:

1. `GMAIL_ADDRESS` spelled exactly right in `.env` — a one-letter typo here once caused an hours-long misdiagnosis.
2. App Password not revoked — regenerate and update `.env`.
3. `GMAIL_APP_PASSWORD` is the 16-character App Password, **not** the account login password.

```bash
curl -X POST http://localhost:8000/ingestion/newsletters   # trigger a run
grep -i "\[Gmail\]" logs/api.error.log | tail -20          # see what happened
```

### 4.5 Ollama timeouts / extraction returning nothing

```bash
ollama ps                                 # what's loaded right now
ollama list                               # what's installed
curl http://localhost:11434/api/tags      # is Ollama even up?
```

Ollama.app auto-starts at login. If it's down, open the app. The two models that must exist:

- `qwen2.5:7b-instruct` — extraction (hot path)
- `qwen3:14b` — reasoning
- `nomic-embed-text` — embeddings

If extraction returns empty arrays, confirm `qwen2.5:7b-instruct` is present and `OLLAMA_EXTRACT_MODEL` points at it.

### 4.6 A run seems stuck

```bash
curl -s http://localhost:8000/ingestion/status | python3 -m json.tool | head -40
curl -X POST http://localhost:8000/ingestion/stop         # cancel current run
```

`gpu_locked: true` with no `current_run` progressing for a long time means a job is wedged. Stopping is best-effort — an Ollama call already in flight will finish first. Restarting the API (§4.1) always clears it.

### 4.7 Everything looks broken and you want a clean slate

Safe, in this order:

```bash
cd ~/vc-scouting-system/vc-scouting-system
docker compose up -d
launchctl kickstart -k gui/$(id -u)/com.vcscouting.api
sleep 15 && curl -s http://localhost:8000/health
```

This touches **no data**. Postgres and Qdrant keep their volumes.

---

## 5. The Review Inbox (the one thing that needs a human)

The pipeline never silently overwrites existing startup data. Every field change and possible duplicate is **staged** for review at `http://<mac-ip>:8000/dashboard` → Review Inbox.

**Currently ~872 pending.** This grows while you're away — that is expected and harmless. Nothing degrades if you ignore it for a month; the queue is a backlog, not an error state.

Markers: 🔴 conflict (would change a populated field) · 🟡 new info (fills a blank) · ⚠️ anomaly. Shortcuts: `j`/`k` navigate, `a` approve, `r` reject. Reject is remembered, so the same thing is not re-flagged next sweep.

**Bulk tools** (all default to a preview/dry-run and show real counts before writing):

```bash
python3 scripts/drain_review_backlog.py              # preview
python3 scripts/drain_review_backlog.py --apply      # execute

python3 scripts/clear_stale_dedup_reviews.py         # preview
python3 scripts/clear_stale_dedup_reviews.py --apply # execute
```

> 🔒 **Safety rule, non-negotiable.** When several pending reviews propose *different* values for the same field on the same record, nothing auto-applies — it goes to a human. This exists because a cleanup script once clobbered 130 fields across 92 startups by trusting stale snapshots. Every bulk path now groups by (record, field) first and refuses ties. Do not write a script that bypasses this.

---

## 6. Command reference

### Services

```bash
docker compose up -d                          # start Postgres + Qdrant + SearXNG
docker compose restart qdrant                 # restart one service
docker ps                                     # status
launchctl kickstart -k gui/$(id -u)/com.vcscouting.api   # restart API
launchctl list | grep -E "vcscouting|gthub"   # service status
```

### Ingestion (via API — preferred; queues on the GPU mutex)

```bash
curl -X POST http://localhost:8000/ingestion/rss
curl -X POST http://localhost:8000/ingestion/newsletters
curl -X POST http://localhost:8000/ingestion/scrape-accelerators
curl -X POST http://localhost:8000/ingestion/scrape-universities
curl -X POST http://localhost:8000/ingestion/run-all          # full sweep
curl -X POST http://localhost:8000/ingestion/stop             # cancel
curl -s http://localhost:8000/ingestion/status                # live progress

# one specific source
curl -X POST http://localhost:8000/ingestion/targeted \
  -H 'Content-Type: application/json' -d '{"source_id":"munich_startup"}'
```

### Ingestion (CLI — only when the API is **not** running)

```bash
python3 scripts/run_ingestion.py rss|accelerators|universities|all
```

> ⚠️ Never run the CLI while the API server is up. The GPU mutex is in-process and cannot guard a second process — you'd have two jobs fighting over Ollama.

### Verification

```bash
curl -X POST "http://localhost:8000/verification/recheck?limit=20"     # local only
curl -X POST "http://localhost:8000/verification/web-verify?limit=10"  # uses web search
curl -s http://localhost:8000/verification/status
```

### Press monitor

```bash
python3 -m press_monitor.run_daily            # run now (sends a real email)
tail -40 logs/pressmonitor.log
```

### Maintenance (all dry-run by default)

```bash
python3 scripts/drain_review_backlog.py [--apply]        # drain field-update backlog
python3 scripts/clear_stale_dedup_reviews.py [--apply]   # re-check duplicate backlog
python3 scripts/dedup_sweep.py [--apply]                 # merge duplicate records
python3 scripts/llm_explain.py --limit 30                # explain pending reviews
python3 scripts/apply_empty_field_reviews.py [--apply]   # fill uncontested blanks
```

### Tests

```bash
python3 -m pytest                    # full suite — 344 tests, ~17s
python3 -m pytest -k dedup           # by keyword
python3 -m pytest tests/test_reviews.py
```

Run the suite after **any** change to matching, storage, scoring, or config loaders. It runs against live Postgres/Qdrant/Ollama; test data is namespaced `PYTEST` and purged automatically — real records are never touched.

### Sources (no restart needed — `config/sources.yaml` is re-read every run)

```bash
curl -s http://localhost:8000/sources
curl -X DELETE http://localhost:8000/sources/web/<source_id>
```

Or use the dashboard's Sources page. A malformed entry is skipped and logged, never crashes a run.

---

## 7. Known issues that are *not* bugs

Don't spend time on these:

- **`MuPDF error: cannot find XObject resource 'img48'`** in the press-monitor log. One page of some e-paper editions has a corrupt image reference in the publisher's own PDF. Investigated 13 Aug: unfixable from our side (two PyMuPDF repair strategies failed; the page renders but yields no text). Costs at most one unscanned page out of ~40. The digest still sends.
- **Review queue growing.** Expected. See §5.
- **`com.vcscouting.dockerstack` / `com.gthub.pressmonitor` showing `-`** in `launchctl list`. Normal for one-shot jobs.
- **Log files at ~55 MB each** (`logs/api.log`, `logs/api.error.log`). No rotation is configured, but growth is ~55 MB/month against 764 GB free. Harmless. Truncate any time with `: > logs/api.log` if you want them tidy.
- **Unused Ollama models** (`qwen3.5:9b`, `gemma4:12b`, `qwen3:8b`, ~19 GB) left over from model A/B testing. Safe to remove with `ollama rm <name>` — the system does not use them.

---

## 8. Configuration & secrets

All settings live in `config/__init__.py` (pydantic-settings), overridden by `.env`.

**`.env` is gitignored and contains real secrets. Never commit it, never paste its contents anywhere.** Keys used:

| Variable | Purpose |
|---|---|
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | Newsletter reading (IMAP) + digest sending (SMTP) |
| `EPAPER_EMAIL` / `EPAPER_PASSWORD` | Memminger Zeitung subscriber login |
| `PRESS_MONITOR_RECIPIENTS` | Comma-separated digest recipients |
| `TAVILY_API_KEY` | Primary web-search backend |
| `DATABASE_URL` | Postgres connection |
| `OLLAMA_EXTRACT_MODEL` / `OLLAMA_REASON_MODEL` | Model selection |

**Web search has a three-step fallback**, so an exhausted quota does not stop the pipeline:
`Tavily` (paid, 1000 free/month) → `SearXNG` (self-hosted, no quota, no key) → `DuckDuckGo` (scrape, fragile).

Recommended Ollama environment (shell profile or `.env`):

```bash
export OLLAMA_KEEP_ALIVE=5m          # unload idle models
export OLLAMA_MAX_LOADED_MODELS=2    # 7B extract + 14B reason together
export OLLAMA_NUM_PARALLEL=1         # one request at a time — prevents GPU oversubscription
```

---

## 9. Hard rules

Things that will break the system or corrupt data if ignored.

- ❌ **All inference stays local.** Never configure a cloud LLM provider. The only sanctioned outbound calls are web *search* (§8) and the e-paper/Gmail logins.
- ❌ **Never run the ingestion CLI while the API is running** (§6).
- ❌ **Never auto-apply a field value when multiple pending reviews disagree** (§5).
- ❌ **Never commit `.env` or anything in `credentials/`.**
- ❌ **Don't re-run `scripts/migrate_reviews.py`** unless you mean it. It drops and recreates the review table. It now refuses when rows exist, but don't test that.
- ✅ **Do a reboot rehearsal before a long absence:** `sudo shutdown -r now`, wait, log in, then run §1 with nobody touching a terminal. This exact test is what caught the Docker restart problem.

Also required for unattended survival (macOS settings, not code):

- No sleep: `sudo pmset -c sleep 0 displaysleep 0 disksleep 0`
- Docker Desktop → Settings → General → "Start Docker Desktop when you log in"
- **Automatic login** enabled — a launchd *Agent* never runs without a logged-in GUI session. A reboot without auto-login sits at the lock screen and nothing starts.

---

## 10. Dashboard

`http://<mac-ip>:8000/dashboard` — currently `http://172.16.14.226:8000/dashboard` (verify with `ipconfig getifaddr en0`; the IP can change after a reboot). Office LAN only.

Five pages: **Overview** (KPIs, charts) · **Browse & Search** (keyword + semantic, edit, delete, CSV export) · **Review Inbox** (§5) · **Ingestion** (trigger any job, live counters) · **Sources** (add/remove without editing YAML). Plus a **Regional** register page for the local SME/membership dataset.

⌘K opens a command palette. Light/dark follows the OS.

---

## 11. Escalation

If the system is down and §4 didn't fix it:

1. Capture the evidence before changing anything further:
   ```bash
   tail -200 logs/api.error.log > /tmp/api_err.txt
   docker ps -a > /tmp/docker.txt
   launchctl list | grep -E "vcscouting|gthub" > /tmp/launchd.txt
   ```
2. The data is safe. Postgres and Qdrant use named Docker volumes that survive container removal, restarts, and reboots.
3. Worst case, the whole stack can be rebuilt from the repo: `docker compose up -d`, `python3 scripts/setup_db.py`, reinstall the launchd agents from `launchd/*.plist`.

```bash
# reinstalling launchd agents after a rebuild
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vcscouting.dockerstack.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vcscouting.api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gthub.pressmonitor.plist
```
