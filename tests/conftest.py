"""
Shared pytest fixtures.

These are INTEGRATION tests: they run against the live Postgres + Qdrant + Ollama
(embeddings) so they exercise the real wiring, not mocks. All test data is
namespaced with the "PYTEST" prefix and removed before AND after every test, so
the 91 real startups are never touched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

PREFIX = "PYTEST"


def _purge():
    """Delete every PYTEST-namespaced startup + its dependent review/suppression
    /change-log rows + Qdrant points. Safe: only touches PYTEST-prefixed data."""
    from database.connection import SessionLocal
    from database.models import (DuplicateReview, FieldChange, Startup,
                                 SuppressedMatch)
    from vector_db.qdrant_store import qdrant_store

    db = SessionLocal()
    try:
        ids = [s.id for s in db.query(Startup).filter(Startup.name.like(f"{PREFIX}%")).all()]
        # reviews referencing those startups, or named with the prefix
        db.query(DuplicateReview).filter(
            (DuplicateReview.master_name.like(f"{PREFIX}%")) |
            (DuplicateReview.incoming_name.like(f"{PREFIX}%")) |
            (DuplicateReview.master_id.in_(ids)) |
            (DuplicateReview.incoming_id.in_(ids))
        ).delete(synchronize_session=False)
        db.query(SuppressedMatch).filter(
            (SuppressedMatch.master_id.in_(ids)) | (SuppressedMatch.other_id.in_(ids))
        ).delete(synchronize_session=False)
        # Change-log rows too (added 29 Sep). Ids here are derived from
        # name+website (deterministic uuid5), so a test that re-creates the
        # same PYTEST company gets the SAME id — and without this, history
        # rows from previous runs accumulated under it. That made a test
        # asserting "this field changed once" pass alone and fail in the
        # full suite, and quietly leaked test rows into a real audit table.
        db.query(FieldChange).filter(FieldChange.startup_id.in_(ids)).delete(
            synchronize_session=False)
        for sid in ids:
            db.query(Startup).filter(Startup.id == sid).delete(synchronize_session=False)
        db.commit()
        for sid in ids:
            try:
                qdrant_store.delete_startup(str(sid))
            except Exception:
                pass
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean_around():
    """Purge PYTEST data before and after each test."""
    _purge()
    yield
    _purge()


@pytest.fixture
def db():
    from database.connection import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def make():
    """
    Factory: upsert a PYTEST startup and return (record_id, status).
    Auto-prefixes the name so cleanup catches it. Usage:
        rid, status = make(name="Alpha", website="alpha.com", city="Munich")
    """
    from processing.storage import upsert_startup

    def _make(name, source="pytest", source_url=None, **fields):
        payload = {"name": f"{PREFIX} {name}", **fields}
        url = source_url or f"https://pytest/{name}".replace(" ", "_")
        return upsert_startup(payload, source=source, source_url=url,
                              published_date=fields.get("published_date"))
    return _make
