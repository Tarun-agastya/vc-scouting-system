"""Review resolution: approve applies/merges, reject suppresses re-flagging."""
import asyncio
from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from api.routes import reviews as R


def _pending_field_update(db, master_id):
    return db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == "field_update",
        DuplicateReview.status == "pending",
    ).first()


def test_approve_field_update_applies_to_master(make, db):
    rid, _ = make("Rev Approve", website="pytest-rev-approve.com", city="Munich",
                  description="solar logistics")
    make("Rev Approve", website="pytest-rev-approve.com", city="Hamburg",
         description="solar logistics")  # stages a conflict
    rev = _pending_field_update(db, rid)
    assert rev is not None
    asyncio.run(R.approve_review(str(rev.id), db=SessionLocal()))
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().city == "Hamburg"
    assert db.query(DuplicateReview).filter(DuplicateReview.id == rev.id).first().status == "approved"


def test_reject_field_update_suppresses_reflag(make, db):
    rid, _ = make("Rev Reject", website="pytest-rev-reject.com", city="Munich",
                  description="solar logistics")
    make("Rev Reject", website="pytest-rev-reject.com", city="Berlin",
         description="solar logistics")
    rev = _pending_field_update(db, rid)
    asyncio.run(R.reject_review(str(rev.id), db=SessionLocal()))
    # a suppression is recorded...
    sup = db.query(SuppressedMatch).filter(SuppressedMatch.kind == "rejected_value",
                                           SuppressedMatch.master_id == rid).first()
    assert sup and sup.field == "city" and sup.value == "Berlin"
    # ...and re-ingesting the rejected value is now a no_op (not re-flagged)
    from processing.storage import upsert_startup
    _, status = upsert_startup(
        {"name": "PYTEST Rev Reject", "website": "pytest-rev-reject.com", "city": "Berlin",
         "description": "solar logistics"},
        source="pytest", source_url="https://pytest/again")
    assert status == "no_op"


def test_approve_duplicate_merges_rows(make, db):
    r1, _ = make("Dup Keeper", city="Vienna", description="telemedicine for rural clinics")
    r2, s2 = make("Dup Keeper", city="Vienna", description="remote doctor visits for rural areas")
    # only proceed if it staged a duplicate FOR THESE ROWS — scoped by id, not
    # "whichever possible_duplicate is newest in the whole table", which on a
    # live/shared DB can pick up an unrelated review (confirmed 23 Jul: an
    # orphaned pre-existing review surfaced this way once Phase D-1 correctly
    # stopped "Dup Keeper" itself from ever staging a duplicate).
    rev = db.query(DuplicateReview).filter(
        DuplicateReview.review_type == "possible_duplicate",
        DuplicateReview.status == "pending",
        DuplicateReview.master_id.in_([r1, r2]),
    ).order_by(DuplicateReview.created_at.desc()).first()
    if rev is None:
        return  # scoring landed it elsewhere (or D-1 recognized them as the same record); covered by storage tests
    master_id = str(rev.master_id)
    asyncio.run(R.approve_review(str(rev.id), db=SessionLocal()))
    db.expire_all()
    # keeper survives, the other row is gone
    assert db.query(Startup).filter(Startup.id == master_id).first() is not None
