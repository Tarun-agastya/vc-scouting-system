import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from api.routes import scout, matchmaking, ingestion, sources, reviews, verification
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
        from apscheduler.triggers.cron import CronTrigger
        from processing.scout_controller import scout_controller

        def _gmail_credentials_present() -> bool:
            import os
            return os.path.exists("credentials/gmail_credentials.json")

        async def _scheduled_full_sweep():
            """
            The twice-weekly full sweep: RSS -> accelerators -> universities ->
            newsletters, run sequentially through the controller (each source
            acquires the GPU mutex in turn, so nothing overlaps).
            """
            logger.info("[Scheduler] Starting twice-weekly full sweep")
            await scout_controller.run_all()

        async def _scheduled_gmail_topup():
            """
            Daily incremental Gmail check, independent of the twice-weekly full
            sweep, so newsletters arriving between sweeps don't wait days to be
            picked up. Cheap: only processes messages not already in the
            incremental-fetch state file. Silently skips if OAuth credentials
            are missing — not a blocker.
            """
            if not _gmail_credentials_present():
                logger.debug("[Gmail] Credentials not found — skipping scheduled top-up")
                return
            await scout_controller.run_newsletters(max_messages=50)

        async def _scheduled_llm_explain():
            """
            Nightly: the local 14B model writes a plain-language explanation for
            each pending duplicate/field-update review (never a verdict). Runs
            one at a time under the GPU mutex so it never fights ingestion.
            Degrades cleanly if Ollama is down.
            """
            from processing.review_explainer import explain_pending_reviews
            await explain_pending_reviews(limit=30)

        async def _scheduled_recheck():
            """
            Nightly (Phase H-3): re-verify a batch of the unverified backlog
            against its own source text — through the controller so it shows
            in /ingestion/status like any run. Self-drains the backlog over
            successive nights without anyone pressing "Recheck now".
            """
            from processing.scout_controller import scout_controller
            await scout_controller.run_recheck(limit=30)

        scheduler = AsyncIOScheduler()

        # Full sweep: Monday + Thursday at 05:00 (twice a week, per the 25 June
        # requirements). Off-hours so it never competes with anyone using the
        # dashboard/agent during the day.
        scheduler.add_job(
            func=_scheduled_full_sweep,
            trigger=CronTrigger(day_of_week="mon,thu", hour=5, minute=0),
            id="full_sweep",
            replace_existing=True,
        )

        # Gmail top-up: daily at 13:00, offset from the 05:00 full sweep so it
        # never fires at the same moment on Mon/Thu.
        scheduler.add_job(
            func=_scheduled_gmail_topup,
            trigger=CronTrigger(hour=13, minute=0),
            id="gmail_topup",
            replace_existing=True,
        )

        # LLM review explanations: nightly at 02:00 (quiet hours, no sweep then).
        scheduler.add_job(
            func=_scheduled_llm_explain,
            trigger=CronTrigger(hour=2, minute=0),
            id="llm_explain",
            replace_existing=True,
        )

        # Verification recheck (Phase H-3): nightly at 03:00, after the 02:00
        # explain job so the two nightly LLM jobs don't overlap.
        scheduler.add_job(
            func=_scheduled_recheck,
            trigger=CronTrigger(hour=3, minute=0),
            id="verification_recheck",
            replace_existing=True,
        )

        scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "Background scheduler started (full sweep Mon+Thu 05:00, Gmail top-up daily 13:00, "
            "LLM review explanations nightly 02:00, verification recheck nightly 03:00)"
        )
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
app.include_router(sources.router,     prefix="/sources",     tags=["Sources"])
app.include_router(reviews.router,     prefix="/reviews",     tags=["Reviews"])
app.include_router(verification.router, prefix="/verification", tags=["Verification"])


@app.get("/health", tags=["System"])
async def health():
    """Quick health check — also returns startup count."""
    try:
        from vector_db.qdrant_store import qdrant_store
        count = qdrant_store.get_startup_count()
    except Exception:
        count = -1
    return {"status": "ok", "startups_in_db": count}


# ── Team dashboard (static SPA) ───────────────────────────────────────────────
# Served by this same process so there is no second service, no CORS, and it is
# reachable on the office LAN the moment the API is up. Mounted AFTER the API
# routers so a static path can never shadow an endpoint.
_UI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "static"
)
if os.path.isdir(_UI_DIR):
    app.mount("/dashboard", StaticFiles(directory=_UI_DIR, html=True), name="dashboard")
    logger.info("Team dashboard mounted at /dashboard")
else:
    logger.warning(f"Dashboard directory not found at {_UI_DIR} — /dashboard disabled")


@app.get("/", include_in_schema=False)
async def root():
    """Root previously 404'd — send people to the dashboard."""
    return RedirectResponse(url="/dashboard/")
