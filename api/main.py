import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from api.routes import scout, matchmaking, ingestion, sources, reviews, verification, classification, theses, regional
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
            # Fixed 13 Aug 2026: this used to check for the OLD OAuth
            # client-secret file (credentials/gmail_credentials.json), which
            # config/__init__.py's own gmail_credentials_path docstring
            # already documents as unused since the App Password migration
            # (Phase PM, 12 Aug). It only "worked" by coincidence — that
            # leftover file still happened to exist on disk. Deleting it as
            # apparent cleanup (a reasonable thing to do given it's
            # documented as unused) would have silently disabled the daily
            # Gmail top-up forever, even with valid App Password credentials
            # in .env. Now checks the credentials that actually gate
            # ingestion/gmail_auth.py's IMAP/SMTP login.
            from config import settings
            return bool(settings.gmail_address and settings.gmail_app_password)

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
            incremental-fetch state file. Silently skips if the App Password
            credentials are missing — not a blocker.
            """
            if not _gmail_credentials_present():
                logger.debug("[Gmail] GMAIL_ADDRESS/GMAIL_APP_PASSWORD not configured — skipping scheduled top-up")
                return
            await scout_controller.run_newsletters_then_recheck(max_messages=50)

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

            limit=80 (raised from 30, 28 Jul): headroom so a month of
            unattended Mon/Thu sweeps can't outpace the nightly drain and let
            the unverified backlog quietly grow. It runs at 03:00 with nothing
            else competing for the GPU, and the consecutive-timeout guard now
            keeps a slow record from aborting the batch, so a larger limit is
            safe — it just processes more each night, stopping early when the
            backlog is empty.
            """
            from processing.scout_controller import scout_controller
            await scout_controller.run_recheck(limit=80)

        async def _scheduled_web_verify():
            """
            Nightly (Phase W, automated 4 Aug at the owner's explicit
            request — originally deliberately manual-only pending real
            usage data on WebSearch cost/quota, per DATA_INTEGRITY_PLAN.md
            Addendum 6; several manual batches have since run cleanly).
            Drains the no-source-excerpt backlog (records H-3's recheck
            structurally cannot reach) a bit every night, same self-
            draining shape as _scheduled_recheck. limit=20 — conservative,
            matching the manual-trigger batch size precedent, since a
            search call leaves the machine per record (the one accepted
            exception to the local-only inference invariant, see
            processing/web_verifier.py).
            """
            from processing.scout_controller import scout_controller
            await scout_controller.run_web_verify(limit=20)

        async def _scheduled_reclassify():
            """
            Nightly safety net (Phase V-2 automated 4 Aug): new records are
            already classified inline at ingest, so this is not draining an
            ongoing backlog in normal operation — it exists to catch the
            rare record whose inline classification failed (Ollama hiccup,
            timeout) and was left with classified_at=NULL, rather than
            leaving it permanently uncategorised. limit=20, cheap (7B
            structured-output call only, no search).
            """
            from processing.scout_controller import scout_controller
            await scout_controller.run_reclassify(limit=20)

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

        # Web verification (Phase W): nightly at 04:00, after recheck so the
        # no-source-excerpt backlog (which recheck can't touch) also drains
        # on its own — see _scheduled_web_verify's docstring for why this
        # was manual-only until 4 Aug.
        scheduler.add_job(
            func=_scheduled_web_verify,
            trigger=CronTrigger(hour=4, minute=0),
            id="web_verify",
            replace_existing=True,
        )

        # Reclassification safety net (Phase V-2): nightly at 04:30, after
        # web-verify. See _scheduled_reclassify's docstring — not draining
        # an ongoing backlog in normal operation, just catching inline-
        # classification failures.
        scheduler.add_job(
            func=_scheduled_reclassify,
            trigger=CronTrigger(hour=4, minute=30),
            id="reclassify",
            replace_existing=True,
        )

        scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "Background scheduler started (full sweep Mon+Thu 05:00, Gmail top-up daily 13:00, "
            "LLM review explanations nightly 02:00, verification recheck nightly 03:00, "
            "web verification nightly 04:00, reclassify safety-net nightly 04:30). "
            "The press monitor is a fully separate launchd service (com.gthub.pressmonitor, "
            "daily 08:00) — not part of this scheduler, see press_monitor/README.md."
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
app.include_router(classification.router, prefix="/classification", tags=["Classification"])
app.include_router(theses.router,      prefix="/theses",       tags=["Theses"])
# Phase RC — regional company register. Its own prefix and its own dashboard
# page on purpose: it is a separate dataset from `startups` (established local
# employers for membership outreach, not an investment funnel) and must not be
# buried among them. Reads/writes only `regional_companies`.
app.include_router(regional.router,    prefix="/regional",     tags=["Regional"])


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


class _RevalidatingStaticFiles(StaticFiles):
    """
    Plain StaticFiles sends Last-Modified/ETag but no Cache-Control, which
    leaves the browser free to use heuristic caching — no explicit directive
    means "use your own guess," and that guess is allowed to be "don't even
    ask the server." Found live 14 Aug 2026: a dashboard tab left open
    across several same-day JS deploys (fixes shipped and confirmed correct
    server-side, curl'd directly) kept running the OLD code, because
    navigating the SPA's hash routes (#/browse etc.) never re-fetches the
    already-loaded ES modules — only a real page load does, and heuristic
    caching meant even that wasn't guaranteed to ask. `no-cache` (despite
    the name) doesn't disable caching — it forces a revalidation request on
    every load, which is a cheap conditional GET returning 304 when nothing
    changed (confirmed: ETag stays intact), so this costs nothing when the
    dashboard hasn't changed and guarantees a fresh module the moment it has.
    """
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


_UI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "static"
)
if os.path.isdir(_UI_DIR):
    app.mount("/dashboard", _RevalidatingStaticFiles(directory=_UI_DIR, html=True), name="dashboard")
    logger.info("Team dashboard mounted at /dashboard")
else:
    logger.warning(f"Dashboard directory not found at {_UI_DIR} — /dashboard disabled")


@app.get("/", include_in_schema=False)
async def root():
    """Root previously 404'd — send people to the dashboard."""
    return RedirectResponse(url="/dashboard/")
