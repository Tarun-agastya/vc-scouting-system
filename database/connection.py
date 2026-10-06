from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.models import Base
from config import settings
import logging

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Per-record change history. Installed here, on the sessionmaker, so EVERY
# session in the process is covered — the API, the pipeline and any script run
# from the command line. A bulk merge run from a terminal should land in the
# same timeline as a click in the dashboard, and attaching per-caller would
# eventually miss one. Import is deferred to avoid a cycle: change_log reads
# the models, which this module has already imported.
try:
    from processing.change_log import install as _install_change_log

    _install_change_log(SessionLocal)
except Exception as exc:  # never let history capture stop the app booting
    logger.warning(f"Change-log capture not installed: {exc}")

# Decision ledger (A4): one hook sees every human review decision, for the same
# reason as change-log above — five resolve paths today, and a sixth would
# otherwise silently fall out of the record the trust gate is computed from.
try:
    from processing.trust import install as _install_trust

    _install_trust(SessionLocal)
except Exception as exc:
    logger.warning(f"Decision ledger not installed: {exc}")


def get_db():
    """FastAPI dependency: yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after their table already existed. create_all() creates missing
# TABLES but never missing COLUMNS, so a database (or a restore of an older
# backup) that predates one of these would fail every query touching it —
# including the suppression lookup on the ingest path — until someone
# remembered to run a migration script. Idempotent; add new ones here.
_LATE_COLUMNS = [
    ("suppressed_matches", "expires_at", "TIMESTAMP"),
    ("decision_audits", "source", "VARCHAR(12) DEFAULT 'live'"),
]


def ensure_columns():
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _LATE_COLUMNS:
            if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                logger.warning(f"Added missing column {table}.{column}")


def init_db():
    """Create all database tables, then any columns create_all can't add."""
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    logger.info("Database tables initialized successfully")
