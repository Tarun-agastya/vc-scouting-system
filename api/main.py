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
        from ingestion.rss_parser import rss_parser

        async def _scheduled_rss():
            """
            Wrap the synchronous ingest_feeds() in a thread executor so the
            AsyncIOScheduler never blocks the FastAPI event loop during Qwen
            inference or network I/O.
            """
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: rss_parser.ingest_feeds(max_entries=40)
            )

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            func=_scheduled_rss,
            trigger="interval",
            hours=6,
            id="rss_ingestion",
            replace_existing=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Background scheduler started (RSS every 6 hours)")
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
