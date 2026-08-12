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
from processing.web_verifier import apply_verdict

# _is_empty / split_proposal moved to processing/field_policy.py (Phase Z,
# 12 Aug 2026) so ingest and web-verify can't drift on this logic again —
# see tests/test_field_policy.py for their unit coverage.


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


def test_richer_description_auto_applies_even_over_a_populated_value(make, db):
    """Phase Z-1 (12 Aug): description/short_description never reach the
    Review Inbox anymore, even when they'd overwrite something already
    there -- better_freetext decides, deterministically, no human needed."""
    from database.models import Startup, DuplicateReview

    rid, _ = make("Richer", description="Organische Solarfolien.")
    record = db.query(Startup).filter(Startup.id == rid).first()

    richer = ("Entwickelt und produziert leichte, flexible und transparente "
             "organische Photovoltaik-Filme, die in verschiedene Baustoffe "
             "integriert werden können.")
    outcome = apply_verdict(db, record, results=[], verdict=_verdict(
        ("description", richer)))

    assert outcome == "auto_filled"
    db.refresh(record)
    assert record.description == richer
    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert reviews == [], "a description upgrade must never be staged"


def test_vaguer_description_is_rejected_not_applied(make, db):
    """The other direction of the same rule: a vaguer finding must not
    downgrade a real description just because web verification found it."""
    from database.models import Startup, DuplicateReview

    detailed = ("Knowmanity solves the problem of knowledge loss by using an "
               "AI-driven interview process to convert expert knowledge into "
               "a living digital twin queried via chat with source refs.")
    rid, _ = make("Vaguer", description=detailed)
    record = db.query(Startup).filter(Startup.id == rid).first()

    outcome = apply_verdict(db, record, results=[], verdict=_verdict(
        ("description", "Preserves valuable corporate knowledge.")))

    assert outcome == "verified"  # no findings survived build_proposal's filter
    db.refresh(record)
    assert record.description == detailed
    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    assert reviews == []
