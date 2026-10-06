"""
The decision ledger and the autonomy gate (processing/trust.py, A4).

The property that matters: the gate is EARNED and REVOCABLE, computed from the
ledger at the moment of use — and it can be fooled by nothing the model does
itself (machine closures, multi-field reviews, none_fit, sibling reviews
counted three times).
"""
from datetime import datetime, timedelta

import pytest

from database.models import DecisionAudit, DuplicateReview, Startup
from processing.trust import agreement, autonomy_report, field_autonomy


@pytest.fixture(autouse=True)
def _clean_ledger(db):
    """decision_audits has no PYTEST-named column to purge by; own the rows."""
    yield
    db.query(DecisionAudit).filter(DecisionAudit.field.like("pytest_%")).delete(
        synchronize_session=False)
    db.commit()


def _seed(db, field, n, agreed_count, confidence="high", verdict="prefer", days_ago=1):
    """n distinct decisions (distinct masters), of which agreed_count agreed."""
    import uuid
    for i in range(n):
        db.add(DecisionAudit(review_id=uuid.uuid4(), master_id=uuid.uuid4(), field=field,
                             verdict=verdict, confidence=confidence, agreed=i < agreed_count,
                             decided_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def test_no_history_earns_nothing(db):
    assert field_autonomy(db, "pytest_new_field") is False


def test_29_decisions_never_unlock_however_good(db):
    _seed(db, "pytest_f", 29, 29)
    assert field_autonomy(db, "pytest_f") is False


def test_30_decisions_at_97_percent_unlock(db):
    _seed(db, "pytest_f", 30, 30)              # 100%
    assert field_autonomy(db, "pytest_f") is True


def test_it_is_revoked_the_moment_agreement_slips(db):
    _seed(db, "pytest_f", 30, 30)
    assert field_autonomy(db, "pytest_f") is True
    _seed(db, "pytest_f", 5, 0)                # five disagreements: 30/35 = 85.7%
    assert field_autonomy(db, "pytest_f") is False


def test_old_decisions_age_out_of_the_window(db):
    _seed(db, "pytest_f", 40, 40, days_ago=120)
    assert agreement(db, "pytest_f")["n"] == 0


def test_only_high_confidence_prefer_picks_count(db):
    """The gate is about auto-APPLYING a confident overwrite; agreeing on
    'keep' or on a shaky pick says nothing about that."""
    _seed(db, "pytest_f", 40, 40, confidence="medium")
    _seed(db, "pytest_f", 40, 40, verdict="keep")
    assert field_autonomy(db, "pytest_f") is False


def test_identity_fields_never_unlock(db):
    for f in ("website", "name"):
        _seed(db, f, 50, 50)
    try:
        assert field_autonomy(db, "website") is False
        assert field_autonomy(db, "name") is False
    finally:
        db.query(DecisionAudit).filter(DecisionAudit.field.in_(["website", "name"])).delete(
            synchronize_session=False)
        db.commit()


def test_sibling_reviews_are_one_decision_not_three(db):
    import uuid
    m = uuid.uuid4()
    for _ in range(3):
        db.add(DecisionAudit(review_id=uuid.uuid4(), master_id=m, field="pytest_f",
                             verdict="prefer", confidence="high", agreed=True))
    db.commit()
    assert agreement(db, "pytest_f")["n"] == 1


def test_report_lists_fields_with_their_numbers(db):
    _seed(db, "pytest_f", 30, 30)
    row = next(r for r in autonomy_report(db) if r["field"] == "pytest_f")
    assert row["n"] == 30 and row["rate"] == 1.0 and row["earned"] is True


# ── the hook: what gets recorded when a person resolves a review ────────────

def _review(db, rid, field, new, adj, status="pending", evidence_extra=None):
    ev = {"field_adjudications": {field: adj}, **(evidence_extra or {})}
    r = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Trust",
        incoming_name="x", risk_level="low", status=status, source="pytest",
        proposed_changes={field: {"old": "Old", "new": new}}, evidence=ev)
    db.add(r)
    db.commit()
    return r


def _ledger(db, rid):
    db.expire_all()
    return db.query(DecisionAudit).filter(DecisionAudit.master_id == rid).all()


def test_human_approving_the_models_pick_is_recorded_as_agreement(make, db):
    rid, _ = make("Trust Agree", website="pytest-trust-agree.com", city="Munich")
    r = _review(db, rid, "sub_industry", "HealthTech",
                {"winner": "HealthTech", "confidence": "high", "none_fit": False})
    r.status, r.resolved_at = "approved", datetime.utcnow()
    db.commit()
    rows = _ledger(db, rid)
    assert len(rows) == 1 and rows[0].verdict == "prefer" and rows[0].agreed is True


def test_human_rejecting_the_models_pick_is_recorded_as_disagreement(make, db):
    rid, _ = make("Trust Disagree", website="pytest-trust-disagree.com", city="Munich")
    r = _review(db, rid, "sub_industry", "HealthTech",
                {"winner": "HealthTech", "confidence": "high", "none_fit": False})
    r.status, r.resolved_at = "rejected", datetime.utcnow()
    db.commit()
    assert _ledger(db, rid)[0].agreed is False


def test_rejecting_a_rival_value_agrees_with_the_pick(make, db):
    """Three proposals; the model picked B. The person approving B and
    rejecting A means the review carrying A was rejected — that is agreement."""
    rid, _ = make("Trust Rival", website="pytest-trust-rival.com", city="Munich")
    r = _review(db, rid, "sub_industry", "A",
                {"winner": "B", "confidence": "high", "none_fit": False})
    r.status, r.resolved_at = "rejected", datetime.utcnow()
    db.commit()
    assert _ledger(db, rid)[0].agreed is True


def test_machine_closures_are_never_graded(make, db):
    """The model must not grade its own homework."""
    rid, _ = make("Trust Machine", website="pytest-trust-machine.com", city="Munich")
    r = _review(db, rid, "sub_industry", "HealthTech",
                {"winner": None, "confidence": "high", "none_fit": False},
                evidence_extra={"auto_closed_by": "resolver"})
    r.status, r.resolved_at = "rejected", datetime.utcnow()
    db.commit()
    assert _ledger(db, rid) == []


def test_none_fit_and_multi_field_reviews_record_nothing(make, db):
    rid, _ = make("Trust Skip", website="pytest-trust-skip.com", city="Munich")
    r = _review(db, rid, "sub_industry", "X", {"winner": None, "confidence": "high", "none_fit": True})
    r.status, r.resolved_at = "approved", datetime.utcnow()
    multi = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Trust", incoming_name="x",
        risk_level="low", status="pending", source="pytest",
        proposed_changes={"city": {"old": "a", "new": "b"}, "funding_stage": {"old": "c", "new": "d"}},
        evidence={"field_adjudications": {"city": {"winner": "b", "confidence": "high", "none_fit": False}}})
    db.add(multi)
    db.commit()
    multi.status, multi.resolved_at = "approved", datetime.utcnow()
    db.commit()
    assert _ledger(db, rid) == []


def test_policy_closures_are_never_graded(make, db):
    """Found 30 Sep: draining sub_industry reviews (policy_closed) wrote 23
    'human decisions' into the ledger, because the hook didn't know that
    marker. Every marker a machine closure can carry must be skipped, or the
    autonomy gate is fed agreement nobody gave."""
    rid, _ = make("Trust Policy", website="pytest-trust-policy.com", city="Munich")
    r = _review(db, rid, "sub_industry", "X",
                {"winner": "X", "confidence": "high", "none_fit": False},
                evidence_extra={"policy_closed": "field is never staged: sub_industry"})
    r.status, r.resolved_at = "rejected", datetime.utcnow()
    db.commit()
    assert _ledger(db, rid) == []


def test_every_machine_marker_the_codebase_writes_is_recognised():
    """The list of markers must not drift from the code that writes them."""
    import pathlib, re
    from processing.trust import _MACHINE_MARKERS
    root = pathlib.Path(__file__).resolve().parent.parent
    written = set()
    for f in list((root / "processing").glob("*.py")) + list((root / "scripts").glob("*.py")):
        written |= set(re.findall(r'ev\["(auto_[a-z_]+|policy_closed)"\]', f.read_text()))
    assert written <= set(_MACHINE_MARKERS), f"unrecognised machine markers: {written - set(_MACHINE_MARKERS)}"
