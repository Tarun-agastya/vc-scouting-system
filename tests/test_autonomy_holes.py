"""
Adversarial tests for the autonomy system — each asserts the behaviour that
SHOULD hold, so a failure is a confirmed hole rather than a style nit.

Written 30 Sep as an audit after the first real auto-merge batch found a bug
(a record used as both keeper and loser) that every earlier test had missed.
The lesson was that the tests only exercised the shapes their author thought
of, so these go looking for the shapes nobody did: concurrent human action,
multi-field reviews, bulk clicks, starvation of the queue, and jobs that
silently don't run.

Same isolation rules as the other resolver tests: master_ids on every call,
require_backup=False only ever together with master_ids.
"""
import asyncio
from datetime import datetime, timedelta

import processing.dedup_adjudicator as dedup_adjudicator
import processing.field_adjudicator as field_adjudicator
from database.connection import SessionLocal
from database.models import (DecisionAudit, DuplicateReview, MergeSnapshot,
                             Startup, SuppressedMatch)
from processing.auto_merge import auto_merge_pending
from processing.deduplicator import normalize_company_name
from processing.review_resolver import resolve_pending


def _keep_high(*a, **k):
    return {"winner": None, "none_fit": False, "confidence": "high",
            "reasoning": "stored value fits", "considered": ["x"], "model": "t"}


def _review(db, rid, changes, **kw):
    r = DuplicateReview(review_type="field_update", master_id=rid, master_name="PYTEST holes",
                        incoming_name="x", risk_level="low", status="pending", source="pytest",
                        proposed_changes=changes, **kw)
    db.add(r)
    db.commit()
    return r


# ── H1: a multi-field review must not be closed wholesale ───────────────────

def test_keeping_one_field_does_not_throw_away_the_others(make, db, monkeypatch):
    """
    A review can propose several fields at once (34 mixed tags+scalar rows sat
    in the queue). The resolver judged ONE field 'keep the stored value' and
    then closed the WHOLE review — rejecting, and suppressing, every other
    proposal in it, including a lossless tags union nobody had looked at.
    """
    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", _keep_high)
    rid, _ = make("Holes Multi", website="pytest-holes-multi.com", city="Munich",
                  description="widget maker", tags=["a"])
    r = _review(db, rid, {"city": {"old": "Munich", "new": "Berlin"},
                          "tags": {"old": ["a"], "new": ["a", "b", "c"]}})
    review_id = r.id

    asyncio.run(resolve_pending(50, apply=True, only_type="field_update", master_ids=[rid]))

    db.expire_all()
    rev = db.query(DuplicateReview).get(review_id)
    assert rev.status == "pending", "the whole review was closed over one field"
    assert "tags" in (rev.proposed_changes or {}), "the tags proposal was thrown away"
    values = {(s.field, s.value) for s in db.query(SuppressedMatch).filter(
        SuppressedMatch.master_id == rid).all()}
    assert ("city", "Berlin") in values                  # the judged field IS settled
    assert not any(f == "tags" for f, _ in values)       # ...and nothing else was suppressed


# ── H2: never overwrite a person's decision made while the job was running ──

def test_a_decision_made_by_a_person_mid_run_is_not_overwritten(make, db, monkeypatch):
    """
    The resolver loads every pending review, then spends ~40 minutes on the
    model. A person can approve one meanwhile. The resolver's object is stale
    (still 'pending'), so setting status='rejected' silently flipped an
    approved review to rejected — and suppressed the value they just applied.
    """
    rid, _ = make("Holes Race", website="pytest-holes-race.com", city="Munich",
                  description="widget maker")
    r = _review(db, rid, {"city": {"old": "Munich", "new": "Berlin"}})
    review_id = r.id

    def person_approves_while_model_thinks(*a, **k):
        other = SessionLocal()
        try:
            row = other.query(DuplicateReview).get(review_id)
            row.status, row.resolved_at = "approved", datetime.utcnow()
            other.commit()
        finally:
            other.close()
        return _keep_high()

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", person_approves_while_model_thinks)
    asyncio.run(resolve_pending(50, apply=True, only_type="field_update", master_ids=[rid]))

    db.expire_all()
    assert db.query(DuplicateReview).get(review_id).status == "approved"
    assert db.query(SuppressedMatch).filter(SuppressedMatch.master_id == rid).count() == 0


def test_record_rejection_refuses_an_already_resolved_review(make, db):
    from processing.review_actions import record_rejection
    rid, _ = make("Holes Guard", website="pytest-holes-guard.com", city="Munich")
    r = _review(db, rid, {"city": {"old": "Munich", "new": "Berlin"}})
    r.status = "approved"
    db.commit()
    record_rejection(db, r, by="resolver")
    db.expire_all()
    assert db.query(DuplicateReview).get(r.id).status == "approved"


# ── H3: bulk clicking is not scrutiny ───────────────────────────────────────

def test_bulk_approving_does_not_earn_the_model_autonomy(make, db, monkeypatch):
    """
    The trust gate counts a person agreeing with the model. 'Approve all
    matching filter' is one click over hundreds of reviews — counting each as
    an independent agreement would let rubber-stamping earn auto-apply.
    """
    from api.routes import reviews as R
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    ids = []
    for i in range(3):
        rid, _ = make(f"Holes Bulk {i}", website=f"pytest-holes-bulk-{i}.com", city="Munich")
        ids.append(str(_review(
            db, rid, {"funding_stage": {"old": None, "new": "Seed"}},
            evidence={"field_adjudications": {"funding_stage": {
                "winner": "Seed", "confidence": "high", "none_fit": False}}}).id))

    asyncio.run(R.bulk_approve_reviews(R.BulkReviewRequest(ids=ids), db=SessionLocal()))

    db.expire_all()
    assert db.query(DecisionAudit).filter(DecisionAudit.review_id.in_(ids)).count() == 0


# ── H4: a queue head the resolver can't close must not starve the rest ──────

def test_reviews_already_judged_do_not_starve_the_unjudged(make, db, monkeypatch):
    """
    limit=N takes the N OLDEST pending duplicate reviews. same_company and
    insufficient_evidence verdicts are never closed, so the same head is
    re-judged every night and the newer reviews are never reached.
    """
    seen = []

    def fake_pair(a, b, **k):
        seen.append(str(a.id))
        return {"verdict": "insufficient_evidence", "confidence": "low",
                "key_signal": "-", "reasoning": "t", "model": "t"}

    monkeypatch.setattr(dedup_adjudicator, "adjudicate_pair", fake_pair)
    old_ids, new_id, my_reviews = [], None, []
    for tag in ("old1", "old2", "fresh"):
        a, _ = make(f"Holes Starve {tag} A", website=f"pytest-holes-starve-{tag}-a.com", city="Munich")
        b, _ = make(f"Holes Starve {tag} B", website=f"pytest-holes-starve-{tag}-b.com", city="Berlin")
        ev = {"adjudication": {"verdict": "insufficient_evidence", "confidence": "low",
                               "at": datetime.utcnow().isoformat(timespec="seconds")}} if tag != "fresh" else {}
        rev = DuplicateReview(review_type="possible_duplicate", master_id=a, master_name="x",
                              incoming_id=b, incoming_name="x", risk_level="low", status="pending",
                              source="pytest", evidence=ev,
                              created_at=datetime.utcnow() - timedelta(days=5 if tag != "fresh" else 0))
        db.add(rev)
        db.commit()
        my_reviews.append(rev.id)
        (old_ids if tag != "fresh" else [None]).append(a) if tag != "fresh" else None
        if tag == "fresh":
            new_id = a
    masters = old_ids + [new_id]
    # make() runs the real matcher, which may itself stage reviews between
    # these look-alike records. Keep only the three constructed above so the
    # test is about ordering, not about what the matcher happened to add.
    db.query(DuplicateReview).filter(DuplicateReview.master_id.in_(masters),
                                     ~DuplicateReview.id.in_(my_reviews)).delete(synchronize_session=False)
    db.commit()

    asyncio.run(resolve_pending(1, apply=True, only_type="possible_duplicate", master_ids=masters))

    assert str(new_id) in seen, "the unjudged review was never reached — the judged head starved it"


# ── H7: merging must not silently drop list-valued data ─────────────────────

def test_merge_keeps_tags_from_both_records(make, db):
    """Default merge choices take the keeper's value when it has one — fine
    for a scalar, a silent loss for a list."""
    keeper_id, _ = make("Holes Tags K", website="pytest-holes-tags.com", city="Munich",
                        description="widget maker", tags=["alpha"])
    loser_id, _ = make("Holes Tags L", city="Munich", description="widget maker", tags=["beta"])
    keeper = db.query(Startup).filter(Startup.id == keeper_id).first()
    loser = db.query(Startup).filter(Startup.id == loser_id).first()
    loser.name, loser.normalized_name, loser.fingerprint = keeper.name, normalize_company_name(keeper.name), None
    db.add(DuplicateReview(review_type="possible_duplicate", master_id=keeper_id, master_name="x",
                           incoming_id=loser_id, incoming_name="x", risk_level="low",
                           status="pending", source="pytest"))
    db.commit()
    try:
        auto_merge_pending(5, apply=True, master_ids=[keeper_id], require_backup=False)
        db.expire_all()
        assert set(db.query(Startup).filter(Startup.id == keeper_id).first().tags) == {"alpha", "beta"}
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == keeper_id).delete()
        db.commit()


# ── H10: a test-only escape hatch must not be usable on live data ───────────

def test_skipping_the_backup_gate_requires_scoping_to_specific_records(monkeypatch):
    """require_backup=False exists so tests needn't touch real backups. Called
    without master_ids it would merge REAL data with no restore point.

    This test originally called it that way to prove the point — and, with no
    guard yet in place, merged 5 live pairs. It now stubs the scan to blow up,
    so a missing guard fails the test instead of touching real records."""
    import processing.auto_merge as am

    def must_not_scan(*a, **k):
        raise AssertionError("the guard let an unscoped, backup-less run reach live data")

    monkeypatch.setattr(am, "find_eligible", must_not_scan)
    stats = auto_merge_pending(5, apply=True, require_backup=False)      # no master_ids
    assert "blocked" in stats and stats["merged"] == 0


# ── H5 / H11: the nightly chain must survive a hiccup and a fresh schema ────

def test_scheduler_retries_a_missed_run_instead_of_skipping_the_night():
    """APScheduler's default misfire grace is 1 second: an event-loop stall at
    01:00 skips the whole night's run, silently, with nothing to notice."""
    import inspect
    import api.main as m
    src = inspect.getsource(m)
    assert "misfire_grace_time" in src and "coalesce" in src


def test_startup_adds_columns_that_create_all_cannot():
    """create_all makes missing TABLES, never missing COLUMNS. Two columns were
    added by ALTER scripts today; an older database (or a restore of an older
    backup) would break every suppression lookup until someone remembered."""
    from database.connection import ensure_columns
    ensure_columns()
    ensure_columns()                                           # idempotent


# ── H12: after a merge, the same page must not re-create the duplicate ──────

def test_recrawling_a_page_after_a_merge_does_not_regrow_the_duplicate(make, db):
    page = "https://pytest.example/holes-regrow"
    keeper_id, _ = make("Holes Regrow K", website="pytest-holes-regrow.com", city="Munich",
                        description="widget maker", source_url=page)
    loser_id, _ = make("Holes Regrow L", city="Munich", description="widget maker", source_url=page)
    keeper = db.query(Startup).filter(Startup.id == keeper_id).first()
    loser = db.query(Startup).filter(Startup.id == loser_id).first()
    name = keeper.name
    loser.name, loser.normalized_name, loser.fingerprint = name, normalize_company_name(name), None
    db.add(DuplicateReview(review_type="possible_duplicate", master_id=keeper_id, master_name=name,
                           incoming_id=loser_id, incoming_name=name, risk_level="low",
                           status="pending", source="pytest"))
    db.commit()
    try:
        auto_merge_pending(5, apply=True, master_ids=[keeper_id], require_backup=False)
        # The same page is crawled again and yields the website-less copy once more.
        rid2, status2 = make("Holes Regrow K", city="Munich", description="widget maker",
                             source_url=page)
        assert rid2 == keeper_id
        assert status2 in ("no_op", "staged_update")
        db.expire_all()
        assert db.query(DuplicateReview).filter(
            DuplicateReview.master_id == keeper_id,
            DuplicateReview.review_type == "possible_duplicate",
            DuplicateReview.status == "pending").count() == 0
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == keeper_id).delete()
        db.commit()


# ── H15: what a person already decided about a record survives its merge ────

def test_suppressions_follow_the_record_through_a_merge(make, db):
    """
    A person once said 'X is a different company from <the merged-away copy>'
    (known_different), or rejected a value for it. The merge deleted that
    copy and left the suppression pointing at nothing, so X was flagged
    against the keeper all over again and the rejected value came back.
    """
    keeper_id, _ = make("Holes Sup K", website="pytest-holes-sup.com", city="Munich",
                        description="widget maker")
    loser_id, _ = make("Holes Sup L", city="Munich", description="widget maker")
    other_id, _ = make("Holes Sup Other", website="pytest-holes-sup-other.com", city="Berlin",
                       description="something else")
    keeper = db.query(Startup).filter(Startup.id == keeper_id).first()
    loser = db.query(Startup).filter(Startup.id == loser_id).first()
    loser.name, loser.normalized_name, loser.fingerprint = keeper.name, normalize_company_name(keeper.name), None
    db.add_all([
        DuplicateReview(review_type="possible_duplicate", master_id=keeper_id, master_name="x",
                        incoming_id=loser_id, incoming_name="x", risk_level="low",
                        status="pending", source="pytest"),
        SuppressedMatch(kind="known_different", master_id=loser_id, other_id=other_id),
        SuppressedMatch(kind="rejected_value", master_id=loser_id, field="city", value="Hamburg"),
    ])
    db.commit()
    try:
        auto_merge_pending(5, apply=True, master_ids=[keeper_id], require_backup=False)
        db.expire_all()
        assert db.query(SuppressedMatch).filter(
            SuppressedMatch.kind == "known_different", SuppressedMatch.master_id == keeper_id,
            SuppressedMatch.other_id == other_id).count() == 1
        assert db.query(SuppressedMatch).filter(
            SuppressedMatch.kind == "rejected_value", SuppressedMatch.master_id == keeper_id,
            SuppressedMatch.field == "city", SuppressedMatch.value == "Hamburg").count() == 1
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == keeper_id).delete()
        db.commit()
