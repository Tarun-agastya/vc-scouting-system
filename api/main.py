import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import scout, matchmaking, ingestion
from database.connection import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("Initializing VC Scouting Intelligence System...")

    # Create PostgreSQL tables
    init_db()

    # Ensure Qdrant collections exist
    try:
        from vector_db.qdrant_store import qdrant_store
        qdrant_store.ensure_collections()
    except Exception as exc:
        logger.warning(f"Qdrant not reachable yet — will retry on first request: {exc}")

    # Start background scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from datetime import timedelta
        from processing.scout_controller import scout_controller

        async def _scheduled_rss():
            """Run RSS ingestion through the controller (queues on the GPU mutex)."""
            await scout_controller.run_rss(max_entries=40)

        async def _scheduled_gmail():
            """
            Run Gmail newsletter ingestion through the controller.
            Staggered 30 min after start, then every 8 h, so it never
            overlaps with the RSS job (every 6 h from start).
            Silently skips if OAuth credentials are missing — not a blocker.
            """
            import os
            if not os.path.exists("credentials/gmail_credentials.json"):
                logger.debug("[Gmail] Credentials not found — skipping scheduled run")
                return
            await scout_controller.run_newsletters(max_messages=50)

        scheduler = AsyncIOScheduler()

        # RSS: fire immediately on first interval, then every 6 h
        scheduler.add_job(
            func=_scheduled_rss,
            trigger="interval",
            hours=6,
            id="rss_ingestion",
            replace_existing=True,
        )

        # Gmail: first run 30 min after startup, then every 8 h
        # (staggered so it never starts at the same time as an RSS run)
        from datetime import datetime as _dt
        scheduler.add_job(
            func=_scheduled_gmail,
            trigger="interval",
            hours=8,
            start_date=_dt.now() + timedelta(minutes=30),
            id="gmail_ingestion",
            replace_existing=True,
        )

        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Background scheduler started (RSS every 6 h, Gmail every 8 h +30 min offset)")
    except Exception as exc:
        logger.warning(f"Scheduler could not start: {exc}")

    logger.info("System ready. API docs at http://localhost:8000/docs")
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown(wait=False)
    logger.info("System shutdown complete")


app = FastAPI(
    title="VC Scouting Intelligence System",
    description=(
        "AI-powered startup discovery, analysis, and investor matchmaking platform. "
        "Powered by Qwen3:14b + Qdrant + PostgreSQL."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scout.router,       prefix="/scout",       tags=["Scouting"])
app.include_router(matchmaking.router, prefix="/matchmaking", tags=["Matchmaking"])
app.include_router(ingestion.router,   prefix="/ingestion",   tags=["Ingestion"])


@app.get("/health", tags=["System"])
async def health():
    """Quick health check — also returns startup count."""
    try:
        from vector_db.qdrant_store import qdrant_store
        count = qdrant_store.get_startup_count()
    except Exception:
        count = -1
    return {"status": "ok", "startups_in_db": count}
