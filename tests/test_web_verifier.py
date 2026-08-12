"""
Web verification empty-field auto-apply (12 Aug 2026, owner) — the Review
Inbox was being flooded with proposals that were actually uncontested
fills (an empty field, a sourced value found for it — nothing to
adjudicate), not real disagreements. apply_verdict now applies those
directly and only stages a field_update review for genuine conflicts (a
real, non-empty old value contradicted by a new one).

Integration tests against the real Postgres DB (PYTEST-prefixed rows, see
conftest.py) since apply_verdict mutates and commits a real Startup row.
"""
from processing.web_verifier import _is_empty, _split_proposal, apply_verdict


# ── _is_empty / _split_proposal: pure logic ─────────────────────────────────

def test_is_empty_recognizes_none_blank_and_zero():
    assert _is_empty(None) is True
    assert _is_empty("") is True
    assert _is_empty(0) is True


def test_is_empty_does_not_flag_real_values():
    assert _is_empty("Munich") is False
    assert _is_empty("0 employees") is False
    assert _is_empty(2020) is False


def test_split_proposal_separates_fills_from_conflicts():
    proposed = {
        "city": {"old": None, "new": "Munich", "source_url": "https://a.de"},
        "country": {"old": "", "new": "Germany", "source_url": "https://a.de"},
        "funding_stage": {"old": "Seed", "new": "Series A", "source_url": "https://a.de"},
    }
    fills, conflicts = _split_proposal(proposed)
    assert set(fills) == {"city", "country"}
    assert set(conflicts) == {"funding_stage"}


def test_split_proposal_all_fills_no_conflicts():
    proposed = {"city": {"old": None, "new": "Munich", "source_url": "https://a.de"}}
    fills, conflicts = _split_proposal(proposed)
    assert fills and not conflicts


# ── apply_verdict: real Startup row, real DuplicateReview table ────────────

def _verdict(*findings):
    return {"identity_match": True, "summary": "test verdict",
            "findings": [{"field": f, "verdict": "contradicted", "correct_value": v,
                          "source_url": "https://source.example/x"} for f, v in findings]}


def test_pure_fill_is_applied_directly_with_no_review(make, db):
    """Every proposed field was empty beforehand -> applied straight to the
    record, verification_status='verified', and critically: NO
    DuplicateReview row -- that's the whole point of this change."""
    from database.models import Startup, DuplicateReview

    rid, _ = make("Fillonly", city=None, country=None)
    record = db.query(Startup).filter(Startup.id == rid).first()

    outcome = apply_verdict(db, record, results=[], verdict=_verdict(
        ("city", "Munich"), ("country", "Germany")))

    assert outcome == "auto_filled"
    db.refresh(record)
    assert record.city == "Munich"
    assert record.country == "Germany"
    assert record.verification_status == "verified"
    assert record.verification_evidence["reason"] == "web_verified"
    assert set(record.verification_evidence["auto_filled"]) == {"city", "country"}

    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert reviews == [], "an uncontested fill must never create a review row"


def test_real_conflict_still_creates_a_review_for_only_that_field(make, db):
    """A field with an existing, non-empty value being contradicted still
    goes to the Review Inbox -- exactly as before this change."""
    from database.models import Startup, DuplicateReview

    rid, _ = make("Conflictonly", funding_stage="Seed")
    record = db.query(Startup).filter(Startup.id == rid).first()

    outcome = apply_verdict(db, record, results=[], verdict=_verdict(
        ("funding_stage", "Series A")))

    assert outcome == "staged"
    db.refresh(record)
    assert record.funding_stage == "Seed", "a conflicting field must NOT be auto-applied"

    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert len(reviews) == 1
    assert set(reviews[0].proposed_changes) == {"funding_stage"}


def test_mixed_fill_and_conflict_applies_the_fill_and_stages_only_the_conflict(make, db):
    """The realistic case: one field is a clean fill, another is a real
    disagreement. The fill must be applied AND the review must contain
    ONLY the conflicting field, not the whole original proposal."""
    from database.models import Startup, DuplicateReview

    rid, _ = make("Mixed", city=None, funding_stage="Seed")
    record = db.query(Startup).filter(Startup.id == rid).first()

    outcome = apply_verdict(db, record, results=[], verdict=_verdict(
        ("city", "Berlin"), ("funding_stage", "Series A")))

    assert outcome == "staged"
    db.refresh(record)
    assert record.city == "Berlin", "the empty field must be auto-filled even though a sibling conflicts"
    assert record.funding_stage == "Seed", "the conflicting field must stay untouched pending review"

    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert len(reviews) == 1
    assert set(reviews[0].proposed_changes) == {"funding_stage"}, \
        "the review must not re-propose the field that was already auto-applied"
    assert set(record.verification_evidence.get("auto_filled") or {}) == {"city"}


def test_no_findings_at_all_is_unaffected_by_this_change(make, db):
    """Regression guard: a clean check with nothing to fill or contest
    still just marks verified, same as before."""
    from database.models import Startup, DuplicateReview

    rid, _ = make("Clean")
    record = db.query(Startup).filter(Startup.id == rid).first()

    outcome = apply_verdict(db, record, results=[],
                            verdict={"identity_match": True, "summary": "all good", "findings": []})

    assert outcome == "verified"
    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert reviews == []
