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


def get_db():
    """FastAPI dependency: yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all database tables."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialized successfully")
