# VC Scouting Intelligence System

An automated, AI-powered platform for discovering, analyzing, and matching early-stage startups with venture capital investors. The system ingests data from multiple sources (RSS feeds, accelerator websites, university spin-off pages, newsletters), enriches it with local LLM reasoning, stores structured data in PostgreSQL and semantic vectors in Qdrant, and exposes everything through a REST API, a Discord bot, and an OpenClaw AI agent.

---

## Goal

Venture capital deal flow is noisy and manual. Analysts spend hours reading newsletters, scrolling accelerator cohort pages, and maintaining spreadsheets. This system automates that entire pipeline:

1. **Discover** startups automatically from curated sources
2. **Analyze** them with an LLM (sector, stage, traction, red flags)
3. **Match** them against an investor thesis using semantic similarity + rule-based scoring
4. **Surface** results through Discord slash commands or an AI chat agent

The end result is a continuously updated, searchable intelligence layer that lets a VC analyst focus on decisions rather than data collection.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Sources                             │
│  RSS Feeds · Accelerator Sites · University Pages · Gmail       │
└────────────────────────┬────────────────────────────────────────┘
                         │ ingestion layer
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Ingestion Pipeline                           │
│  feedparser (RSS) · trafilatura + Playwright (web scraping)     │
│  Gmail API (newsletters) · Qwen3:14b entity extraction          │
└──────────┬──────────────────────────────────┬───────────────────┘
           │ structured data                  │ text chunks
           ▼                                  ▼
┌──────────────────────┐          ┌───────────────────────────────┐
│   PostgreSQL 15       │          │   Qdrant Vector DB            │
│   (via SQLAlchemy)    │          │   nomic-embed-text 768-dim    │
│   Startups, Founders  │          │   startups + investors        │
│   Rounds, Investors   │          │   collections                 │
└──────────┬───────────┘          └──────────────┬────────────────┘
           │                                     │
           └──────────────┬──────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Backend (port 8000)                 │
│   /scout  ·  /matchmaking  ·  /ingestion  ·  /health           │
└──────────┬──────────────────────────────────────────────────────┘
           │
    ┌──────┴───────┐
    ▼              ▼
Discord Bot     OpenClaw Agent
(slash cmds)    (AI chat interface)
```

---

## Technology Stack

### Backend
| Tool | Version | Why |
|------|---------|-----|
| **FastAPI** | 0.111.0 | Async REST API with automatic OpenAPI docs; plays well with async ingestion tasks |
| **Uvicorn** | latest | ASGI server for FastAPI; handles concurrent requests without blocking |
| **Pydantic v2** | bundled | Request/response validation and settings management via `pydantic-settings` |

### Databases
| Tool | Version | Why |
|------|---------|-----|
| **PostgreSQL 15** | Docker | Structured startup/founder/round data; ACID guarantees; native ARRAY type for tags |
| **SQLAlchemy** | 2.0.30 | Modern async-compatible ORM; declarative models with UUIDs |
| **Qdrant** | Docker (latest) | Purpose-built vector database; cosine similarity search over 768-dim embeddings; much faster than pgvector for semantic search at scale |

### AI / LLM
| Tool | Version | Why |
|------|---------|-----|
| **Ollama** | latest | Runs LLMs locally — no API costs, no data leaving your machine, no rate limits |
| **Qwen3:14b** | via Ollama | Strong reasoning model for entity extraction and startup analysis; `<think>` blocks stripped for clean output |
| **nomic-embed-text** | via Ollama | Lightweight 768-dim embedding model; fast enough for batch ingestion; same host as Qwen so no extra infra |

### Ingestion
| Tool | Version | Why |
|------|---------|-----|
| **feedparser** | latest | Battle-tested RSS/Atom parser for the 11 curated VC news feeds |
| **trafilatura** | latest | Extracts clean article text from HTML pages; far better than raw BeautifulSoup for boilerplate removal |
| **Playwright** | latest | JavaScript-rendered pages (some accelerator sites need it); async, headless Chromium |
| **BeautifulSoup4** | latest | Lightweight HTML parsing fallback for simpler pages |
| **Google API Python Client** | latest | Gmail OAuth2 ingestion; reads newsletter emails directly from inbox |
| **APScheduler** | 3.10.4 | `AsyncIOScheduler` triggers RSS ingestion every 6 hours automatically without a separate cron job |

### Matchmaking
Investor-startup matching uses a **weighted hybrid score**:
- **50% semantic** — cosine similarity between Qdrant startup + investor embeddings
- **20% stage fit** — investor preferred stages vs startup current stage
- **15% industry fit** — overlap between investor thesis sectors and startup sector tags
- **15% geography** — investor regions vs startup location

AI-generated rationale (Qwen3) is produced only for the top 5 matches to avoid unnecessary inference cost.

### Interfaces
| Tool | Version | Why |
|------|---------|-----|
| **discord.py** | 2.3.2 | Slash commands give analysts a zero-friction interface — no web UI to build or maintain |
| **httpx** | latest | Async HTTP client used by Discord bot to call the FastAPI backend |
| **OpenClaw / ElizaOS** | latest | AI agent framework; SCOUT persona wraps the 6 API tools for natural language querying |

### Infrastructure
| Tool | Why |
|------|-----|
| **Docker Compose** | One command brings up PostgreSQL + Qdrant with persistent volumes and healthchecks |
| **python-dotenv / pydantic-settings** | All secrets (DB password, Discord token, etc.) live in `.env`, never in source code |

---

## Project Structure

```
Scouting System/
│
├── api/                        # FastAPI application
│   ├── main.py                 # App factory, lifespan, CORS, scheduler setup
│   └── routes/
│       ├── scout.py            # Startup search, add, list, sector report
│       ├── matchmaking.py      # Investor ↔ startup matching
│       └── ingestion.py        # Trigger ingestion pipelines
│
├── database/
│   ├── models.py               # SQLAlchemy ORM models (Startup, Founder, Investor, etc.)
│   └── connection.py           # Engine, session factory, init_db()
│
├── embeddings/
│   └── embedder.py             # nomic-embed-text via Ollama, 768-dim vectors
│
├── vector_db/
│   └── qdrant_store.py         # Qdrant collections, upsert, cosine search
│
├── reasoning/
│   ├── qwen_client.py          # Qwen3:14b wrapper (cached client, think-block stripping)
│   ├── prompts.py              # All LLM prompt templates (centralized)
│   └── analyzer.py             # Batch analysis, sector reports, background enrichment
│
├── matchmaking/
│   └── engine.py               # Hybrid scoring engine
│
├── ingestion/
│   ├── sources.py              # RSS feeds, accelerator URLs, university URLs
│   ├── rss_parser.py           # RSS → Qwen extraction → Qdrant
│   ├── web_scraper.py          # trafilatura + Playwright scraper
│   └── newsletter_ingestor.py  # Gmail OAuth2 ingestion
│
├── discord_bot/
│   └── bot.py                  # 6 slash commands (/scout /match /sector /add /ingest /status)
│
├── openclaw/
│   ├── character.json          # SCOUT persona definition
│   └── tools_config.json       # 6 tool definitions pointing to localhost:8000
│
├── scripts/
│   ├── setup_db.py             # One-time: create PostgreSQL tables + Qdrant collections
│   └── run_ingestion.py        # CLI: manually trigger ingestion
│
├── config.py                   # Central settings (pydantic-settings, loads .env)
├── docker-compose.yml          # PostgreSQL 15 + Qdrant with volumes
├── requirements.txt            # All Python dependencies
├── .env.example                # Template — copy to .env and fill in secrets
├── start.ps1                   # Windows: start Docker + DB setup + API server
└── start_discord_bot.ps1       # Windows: start Discord bot in separate terminal
```

---

## Data Sources

### RSS Feeds (11 feeds, auto-ingested every 6 hours)
- TechCrunch Startups, EU-Startups, Sifted, VentureBeat
- Gruenderszene, Startup Ticker, EU-Startups Germany focus
- The Recursive (CEE), Estonian Startup Database, Nordic Startup Bits, Startups.be

### Accelerator Cohort Pages (11 sources)
Y Combinator, Techstars, Entrepreneur First, Antler, HTGF, UnternehmerTUM, APX, Founders Factory, Seedcamp, Station F, Wayra

### University Spin-Off Pages (8 sources)
TU Munich, ETH Zurich, Oxford, Cambridge, Imperial College, EPFL, KTH Stockholm, TU Berlin

### Newsletters (via Gmail)
Any VC/startup newsletter delivered to the configured Gmail inbox is automatically parsed.

---

## Discord Bot Commands

| Command | Description |
|---------|-------------|
| `/scout <query>` | Semantic search for startups matching a description |
| `/match <industries> <stages> <regions>` | Find startups matching an investor thesis |
| `/sector <sector>` | Generate an AI sector landscape report |
| `/add <name> <description>` | Manually add a startup to the database |
| `/ingest <source>` | Trigger ingestion: `rss`, `accelerators`, `universities`, or `all` |
| `/status` | Show database stats (startup count, vector count, last ingestion) |

---

## Setup

### Prerequisites
- Docker Desktop running
- Python 3.11+
- Ollama installed with `qwen3:14b` and `nomic-embed-text` pulled
- Discord bot token (for bot)
- Gmail OAuth2 credentials (for newsletter ingestion, optional)

### First-time setup

```powershell
# 1. Clone the repo
git clone https://github.com/Tarun-agastya/vc-scouting-system.git
cd vc-scouting-system

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your DB password, Discord token, etc.

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers
playwright install chromium

# 5. Pull Ollama models
ollama pull qwen3:14b
ollama pull nomic-embed-text

# 6. Start everything (Docker + DB init + API server)
.\start.ps1
```

### Start the Discord bot (separate terminal)
```powershell
.\start_discord_bot.ps1
```

### Manual ingestion
```powershell
python scripts/run_ingestion.py rss           # RSS feeds only
python scripts/run_ingestion.py accelerators  # Accelerator pages
python scripts/run_ingestion.py universities  # University spin-off pages
python scripts/run_ingestion.py all           # Everything
```

---

## Key Design Decisions

**Why local LLM (Ollama) instead of OpenAI?**
Deal flow data is often confidential. Running Qwen3:14b locally means no startup data, founder names, or investor theses are sent to external APIs.

**Why Qdrant instead of pgvector?**
Qdrant is purpose-built for ANN search with hardware-accelerated SIMD operations. For high-volume semantic search across thousands of startup profiles, it outperforms pgvector significantly. PostgreSQL handles structured queries; Qdrant handles similarity search — each does what it's best at.

**Why Discord instead of a web UI?**
VC analysts already live in Discord. A slash command has zero onboarding friction — no new tool to learn, no separate login.

**Why OpenClaw/ElizaOS as the chat agent?**
OpenClaw provides a thin, configurable AI agent layer with tool-calling built in. The SCOUT persona is deliberately minimal (`maxInputTokens: 2500`) to avoid starvation of the main FastAPI event loop — all heavy reasoning stays in the Python process.

**Why APScheduler instead of a cron job?**
APScheduler runs inside the same Python process as FastAPI, requires no OS-level cron configuration, and integrates with the `asyncio` event loop natively. The RSS ingestion job is wrapped in `run_in_executor` so it never blocks the event loop.

---

## Environment Variables

See `.env.example` for all required variables. Key ones:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `QDRANT_HOST` | Qdrant host (default: `localhost`) |
| `QDRANT_PORT` | Qdrant port (default: `6333`) |
| `OLLAMA_BASE_URL` | Ollama API (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | LLM model name (default: `qwen3:14b`) |
| `OLLAMA_EMBED_MODEL` | Embedding model (default: `nomic-embed-text`) |
| `DISCORD_BOT_TOKEN` | Discord bot token from Discord Developer Portal |
| `API_HOST` | FastAPI host (default: `0.0.0.0`) |
| `API_PORT` | FastAPI port (default: `8000`) |
