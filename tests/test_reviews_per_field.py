"""
Per-field resolve — settle ONE field and leave the rest of a review pending.

The bookkeeping rule these lock in: a single review row can propose several
fields, so deciding one of them must not close the row. Each decision lands
in evidence["resolved_fields"][field]; the review closes only once every
field it proposed has been decided, and its proposed_changes is never
mutated so the record of what was originally proposed survives.
"""
import asyncio

from api.routes import reviews as R
from database.connection import SessionLocal
from database.models import DuplicateReview, FieldChange, Startup, SuppressedMatch


def _pending(db, master_id):
    return db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == "field_update",
        DuplicateReview.status == "pending",
    ).all()


def _resolve(master_id, field, value=None, reject=False):
    return asyncio.run(R.resolve_single_field(
        str(master_id), field,
        R.FieldResolveRequest(value=value, reject=reject),
        db=SessionLocal(),
    ))


def _stage(db, master_id, name, proposed):
    r = DuplicateReview(
        review_type="field_update", master_id=master_id, master_name=name,
        incoming_name=name, proposed_changes=proposed,
        risk_level="low", status="pending", source="pytest",
    )
    db.add(r)
    db.commit()
    return r


def test_deciding_one_field_leaves_the_others_pending(make, db, monkeypatch):
    """The whole point: bank a decision on city, keep thinking about
    sub_industry. Before this the only option was all-or-nothing."""
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField Partial", website="pytest-perfield-partial.com",
                  city="Munich", description="widget maker", sub_industry="Fintech")
    r = _stage(db, rid, "PYTEST PerField Partial", {
        "city": {"old": "Munich", "new": "Berlin"},
        "sub_industry": {"old": "Fintech", "new": "HealthTech"},
    })

    res = _resolve(rid, "city", value="Berlin")

    assert res["applied"] is True and res["field"] == "city"
    assert res["still_pending_review_ids"] == [str(r.id)]   # NOT closed
    assert res["approved_review_ids"] == [] and res["rejected_review_ids"] == []

    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().city == "Berlin"   # applied
    assert db.query(Startup).filter(Startup.id == rid).first().sub_industry == "Fintech"  # untouched
    assert len(_pending(db, rid)) == 1                       # still in the queue
    # the original proposal is preserved, only the decision is recorded alongside
    fresh = db.query(DuplicateReview).filter(DuplicateReview.id == r.id).first()
    assert set(fresh.proposed_changes) == {"city", "sub_industry"}
    assert fresh.evidence["resolved_fields"]["city"]["decision"] == "applied"


def test_review_closes_once_every_field_is_decided(make, db, monkeypatch):
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField Closes", website="pytest-perfield-closes.com",
                  city="Munich", description="widget maker", sub_industry="Fintech")
    r = _stage(db, rid, "PYTEST PerField Closes", {
        "city": {"old": "Munich", "new": "Berlin"},
        "sub_industry": {"old": "Fintech", "new": "HealthTech"},
    })

    _resolve(rid, "city", value="Berlin")
    assert len(_pending(db, rid)) == 1     # still open after the first field

    res = _resolve(rid, "sub_industry", reject=True)

    db.expire_all()
    assert _pending(db, rid) == []          # now closed
    # rejected, not approved: one of its two fields was rejected. Same
    # whole-review rule the grouped resolve already uses.
    assert res["rejected_review_ids"] == [str(r.id)]
    assert db.query(Startup).filter(Startup.id == rid).first().sub_industry == "Fintech"


def test_all_fields_applied_closes_the_review_as_approved(make, db, monkeypatch):
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField Approved", website="pytest-perfield-approved.com",
                  city="Munich", description="widget maker", sub_industry="Fintech")
    r = _stage(db, rid, "PYTEST PerField Approved", {
        "city": {"old": "Munich", "new": "Berlin"},
        "sub_industry": {"old": "Fintech", "new": "HealthTech"},
    })

    _resolve(rid, "city", value="Berlin")
    res = _resolve(rid, "sub_industry", value="HealthTech")

    db.expire_all()
    assert res["approved_review_ids"] == [str(r.id)]
    assert _pending(db, rid) == []


def test_reject_suppresses_every_candidate_for_that_field_only(make, db, monkeypatch):
    """A rejected field must not come back on the next crawl — but the
    reject must not leak onto fields this call never looked at."""
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField Suppress", website="pytest-perfield-suppress.com",
                  city="Munich", description="widget maker")
    _stage(db, rid, "PYTEST PerField Suppress", {
        "city": {"old": "Munich", "new": "Berlin"},
        "sub_industry": {"old": None, "new": "HealthTech"},
    })
    _stage(db, rid, "PYTEST PerField Suppress", {"city": {"old": "Munich", "new": "Hamburg"}})

    _resolve(rid, "city", reject=True)

    db.expire_all()
    sups = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "rejected_value", SuppressedMatch.master_id == rid).all()
    by_field = {}
    for s in sups:
        by_field.setdefault(s.field, set()).add(s.value)
    assert by_field.get("city") == {"Berlin", "Hamburg"}   # both candidates suppressed
    assert "sub_industry" not in by_field                   # untouched — never looked at
    assert db.query(Startup).filter(Startup.id == rid).first().city == "Munich"


def test_applying_a_field_records_history(make, db, monkeypatch):
    """The 'while still having that history' half — an applied field lands
    in the change log, attributed, so the per-field history toggle has
    something real to show."""
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField History", website="pytest-perfield-history.com",
                  city="Munich", description="widget maker")
    _stage(db, rid, "PYTEST PerField History", {"city": {"old": "Munich", "new": "Berlin"}})

    _resolve(rid, "city", value="Berlin")

    db.expire_all()
    rows = (db.query(FieldChange)
            .filter(FieldChange.startup_id == rid, FieldChange.field == "city")
            .order_by(FieldChange.changed_at.desc()).all())
    # Assert on the change this test made, not on a total — FieldChange is a
    # shared table and asserting an absolute count couples the test to
    # whatever else has touched this record.
    assert rows, "the applied field wrote no change-log row"
    assert rows[0].old_value == "Munich" and rows[0].new_value == "Berlin"
    assert rows[0].source == "review"

    from processing.change_log import history_for
    only_city = history_for(db, rid, field="city")
    assert only_city and all(h["field"] == "city" for h in only_city)
    assert history_for(db, rid, field="sub_industry") == []   # filter really filters


def test_a_decided_field_disappears_from_the_grouped_view(make, db, monkeypatch):
    """Otherwise a settled field keeps reappearing as a live choice and a
    second decision would re-suppress what was already suppressed."""
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)
    rid, _ = make("PerField Grouped", website="pytest-perfield-grouped.com",
                  city="Munich", description="widget maker", sub_industry="Fintech")
    _stage(db, rid, "PYTEST PerField Grouped", {
        "city": {"old": "Munich", "new": "Berlin"},
        "sub_industry": {"old": "Fintech", "new": "HealthTech"},
    })

    _resolve(rid, "city", value="Berlin")

    result = asyncio.run(R.list_reviews_grouped(status="pending", q="PerField Grouped",
                                                db=SessionLocal()))
    group = next(g for g in result["groups"] if g["master_id"] == str(rid))
    assert set(group["fields"]) == {"sub_industry"}   # city is settled and gone


def test_unknown_field_is_404_not_a_silent_no_op(make, db):
    rid, _ = make("PerField Unknown", website="pytest-perfield-unknown.com", city="Munich")
    _stage(db, rid, "PYTEST PerField Unknown", {"city": {"old": "Munich", "new": "Berlin"}})

    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _resolve(rid, "funding_stage", value="Seed")
    assert exc.value.status_code == 404
