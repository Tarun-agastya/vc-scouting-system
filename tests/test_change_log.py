"""
Per-record change history, captured at one place.

Every field write goes through a session flush, so that is where history is
taken — one before_flush hook rather than a logging call in each writer. A
history that quietly stops being complete looks exactly like a record that
stopped changing, so the capture has to be impossible to bypass rather than
merely well-remembered.
"""
import uuid

import pytest

from database.connection import SessionLocal
from database.models import FieldChange, Startup
from processing.change_log import changes_from, history_for, prune

PREFIX = "PYTEST-CHLOG"


@pytest.fixture
def rec():
    def purge():
        db = SessionLocal()
        try:
            ids = [s.id for s in db.query(Startup).filter(Startup.name.like(f"{PREFIX}%")).all()]
            if ids:
                db.query(FieldChange).filter(
                    FieldChange.startup_id.in_(ids)).delete(synchronize_session=False)
                for i in ids:
                    db.query(Startup).filter(Startup.id == i).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    purge()
    db = SessionLocal()
    sid = str(uuid.uuid4())
    db.add(Startup(id=sid, name=f"{PREFIX} Co", normalized_name=f"{PREFIX} co".lower(),
                   city="Munich", description="first text", industry="Robotics",
                   source="pytest", source_url="https://pytest/chlog"))
    db.commit()
    yield db, sid
    db.close()
    purge()


def _row(db, sid):
    db.expire_all()
    return db.query(Startup).filter(Startup.id == sid).first()


def test_a_field_change_is_recorded_without_the_writer_asking(rec):
    """The whole point: no logging call at the write site."""
    db, sid = rec
    _row(db, sid).city = "Augsburg"
    db.commit()

    h = history_for(db, sid)
    assert len(h) == 1
    assert h[0]["field"] == "city"
    assert h[0]["old"] == "Munich" and h[0]["new"] == "Augsburg"


def test_machine_bookkeeping_is_not_logged(rec):
    """
    TRACKED_FIELDS is an allowlist, not a denylist of noisy fields. A sweep
    touches thousands of scores and timestamps; logging them would bury the
    handful of changes a person can act on.
    """
    db, sid = rec
    row = _row(db, sid)
    row.enrichment_score = 55
    row.score_tier = "INTERESTING"
    db.commit()
    assert history_for(db, sid) == []


def test_rewriting_the_same_value_is_not_a_change(rec):
    db, sid = rec
    _row(db, sid).city = "Munich"          # identical to what is stored
    db.commit()
    assert history_for(db, sid) == []


def test_changes_are_attributed_to_their_cause(rec):
    """
    The hook cannot know WHY something changed. Callers declare it, and the
    flush has to happen inside that block — a commit after the block exits
    records the change but labels it "system", which is worse than useless in
    a timeline whose purpose is saying what caused something.
    """
    db, sid = rec
    with changes_from("merge", detail="merged from 'Other Co'"):
        _row(db, sid).description = "text from the other record"
        db.flush()
    db.commit()

    h = history_for(db, sid)
    assert h[0]["source"] == "merge"
    assert "Other Co" in h[0]["detail"]


def test_an_unattributed_write_is_still_recorded(rec):
    """Dropping it would hide a real change; "system" is a prompt to attribute
    that caller, not a reason to lose the entry."""
    db, sid = rec
    _row(db, sid).website = "https://example.test"
    db.commit()
    assert history_for(db, sid)[0]["source"] == "system"


def test_prune_keeps_the_most_recent_entries(rec):
    db, sid = rec
    for i in range(12):
        _row(db, sid).city = f"City{i}"
        db.commit()
    assert len(history_for(db, sid, limit=200)) == 12

    prune(db, keep_per_record=5)
    left = history_for(db, sid, limit=200)
    assert len(left) == 5
    assert left[0]["new"] == "City11", "the newest must survive"
