"""
processing/review_actions.py — the shared reject contract and the
ResolverRun logger (Phase 4, plans/REVIEW_INBOX_AUTONOMY_PLAN.md).

record_resolver_run has no PYTEST-prefixed name to filter test noise out by
(a run isn't attached to any one startup), so every row this file creates is
tracked and deleted explicitly rather than relying on the usual
clean_around autouse fixture.
"""
from datetime import datetime

from database.models import DuplicateReview, ResolverRun, SuppressedMatch
from processing.review_actions import record_rejection, record_resolver_run


def test_record_rejection_field_update_suppresses_every_proposed_field(make, db):
    rid, _ = make("Actions Reject", website="pytest-actions-reject.com", city="Munich")
    rev = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Actions Reject",
        incoming_name="PYTEST Actions Reject",
        proposed_changes={
            "city": {"old": "Munich", "new": "Berlin"},
            "funding_stage": {"old": None, "new": "Seed"},
        },
        risk_level="low", status="pending", source="pytest",
    )
    db.add(rev)
    db.commit()

    result = record_rejection(db, rev)

    assert result == {"status": "rejected", "review_type": "field_update"}
    db.expire_all()
    assert rev.status == "rejected" and rev.resolved_at is not None
    values = {(s.field, s.value) for s in db.query(SuppressedMatch).filter(
        SuppressedMatch.master_id == rid).all()}
    assert values == {("city", "Berlin"), ("funding_stage", "Seed")}


def test_record_rejection_duplicate_records_known_different(make, db):
    r1, _ = make("Actions Dup A", website="pytest-actions-dupa.com", city="Paris")
    r2, _ = make("Actions Dup B", website="pytest-actions-dupb.com", city="Tokyo")
    rev = DuplicateReview(
        review_type="possible_duplicate", master_id=r1, master_name="PYTEST Actions Dup A",
        incoming_id=r2, incoming_name="PYTEST Actions Dup B",
        risk_level="low", status="pending", source="pytest",
    )
    db.add(rev)
    db.commit()

    record_rejection(db, rev)

    db.expire_all()
    sup = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "known_different",
        SuppressedMatch.master_id == r1, SuppressedMatch.other_id == r2,
    ).first()
    assert sup is not None


def test_record_rejection_commit_false_lets_caller_batch(make, db):
    rid, _ = make("Actions Batch", website="pytest-actions-batch.com", city="Munich")
    rev = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Actions Batch",
        incoming_name="PYTEST Actions Batch",
        proposed_changes={"city": {"old": "Munich", "new": "Berlin"}},
        risk_level="low", status="pending", source="pytest",
    )
    db.add(rev)
    db.commit()

    record_rejection(db, rev, commit=False)
    # not yet committed — a fresh session should not see the suppression
    from database.connection import SessionLocal
    other = SessionLocal()
    try:
        assert other.query(SuppressedMatch).filter(
            SuppressedMatch.master_id == rid).first() is None
    finally:
        other.close()

    db.commit()
    db.expire_all()
    assert db.query(SuppressedMatch).filter(SuppressedMatch.master_id == rid).first() is not None


def test_record_resolver_run_computes_left_pending(db):
    run_id = None
    try:
        started = datetime.utcnow()
        stats = {"keep_current": 3, "prefers_proposal": 2, "none_fit": 1,
                 "unavailable": 4, "auto_closed": 3}
        record_resolver_run("resolve", stats, started)

        row = (db.query(ResolverRun)
               .filter(ResolverRun.kind == "resolve")
               .order_by(ResolverRun.started_at.desc()).first())
        run_id = row.id
        assert row.judged == 6            # 3 + 2 + 1, unavailable/auto_closed excluded
        assert row.auto_closed == 3
        assert row.left_pending == 3       # judged - auto_closed
        assert row.unavailable == 4
        assert row.stats == stats
        assert row.finished_at is not None
    finally:
        if run_id is not None:
            db.query(ResolverRun).filter(ResolverRun.id == run_id).delete()
            db.commit()


def test_record_resolver_run_carries_search_budget_for_research_kind(db):
    run_id = None
    try:
        stats = {"keep_current": 1, "budget_exhausted": 5, "searches_used": 40}
        record_resolver_run("research", stats, datetime.utcnow())

        row = (db.query(ResolverRun)
               .filter(ResolverRun.kind == "research")
               .order_by(ResolverRun.started_at.desc()).first())
        run_id = row.id
        assert row.searches_used == 40
        assert row.judged == 1  # budget_exhausted/searches_used excluded from the verdict sum
    finally:
        if run_id is not None:
            db.query(ResolverRun).filter(ResolverRun.id == run_id).delete()
            db.commit()
