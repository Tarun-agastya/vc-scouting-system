"""Storage staging outcomes — the heart of the data-stewardship model."""
from database.models import Startup, DuplicateReview


def _get(db, sid):
    return db.query(Startup).filter(Startup.id == sid).first()


def test_new_master(make):
    rid, status = make("Stage New", website="pytest-stage-new.com", city="Munich",
                       description="widget maker")
    assert status == "new_master" and rid


def test_identical_reextract_is_no_op(make):
    rid, _ = make("Stage NoOp", website="pytest-stage-noop.com", city="Munich",
                  description="widget maker")
    rid2, status2 = make("Stage NoOp", website="pytest-stage-noop.com", city="Munich",
                         description="widget maker")
    assert status2 == "no_op" and rid2 == rid


def test_blank_fill_auto_applies_directly(make, db):
    """
    Phase Z-3 (12 Aug 2026): an empty-old field has nothing to adjudicate,
    so it's written straight to the master instead of staged — this is the
    fix for the review-queue flood (previously EVERY blank fill, however
    uncontested, became a "low risk" review a human had to click through).
    """
    rid, _ = make("Stage Blank", website="pytest-stage-blank.com", city="Munich",
                  description="widget maker")
    rid2, status2 = make("Stage Blank", website="pytest-stage-blank.com", city="Munich",
                         description="widget maker", funding_stage="Seed")
    assert rid2 == rid
    assert status2 == "no_op"  # nothing staged for a human
    assert _get(db, rid).funding_stage == "Seed"  # but the master WAS updated
    rev = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid,
                                           DuplicateReview.review_type == "field_update").first()
    assert rev is None


def test_conflicting_pending_fill_blocks_auto_apply(make, db):
    """
    Multi-candidate safety guard (Phase Z, 12 Aug) — see
    processing.storage._has_conflicting_pending_fill's docstring for the
    incident (130 fields silently clobbered across 92 startups) this exists
    to prevent. If a pending review already proposes a DIFFERENT value for
    an empty field, a second proposal must be staged, not silently applied
    over whatever the first one eventually resolves to.
    """
    rid, _ = make("Stage Multicand", website="pytest-stage-multicand.com",
                  city="Munich", description="widget maker")

    # Simulate a pending review from an earlier run already proposing a
    # different value for this still-empty field.
    db.add(DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Stage Multicand",
        incoming_name="PYTEST Stage Multicand",
        proposed_changes={"funding_stage": {"old": None, "new": "Pre-Seed"}},
        risk_level="low", status="pending", source="pytest",
    ))
    db.commit()

    rid2, status2 = make("Stage Multicand", website="pytest-stage-multicand.com",
                         city="Munich", description="widget maker", funding_stage="Seed")
    assert rid2 == rid
    assert status2 == "staged_update"
    assert _get(db, rid).funding_stage is None  # NOT auto-applied

    reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).all()
    new_candidates = {r.proposed_changes.get("funding_stage", {}).get("new")
                      for r in reviews if "funding_stage" in (r.proposed_changes or {})}
    assert new_candidates == {"Pre-Seed", "Seed"}  # both candidates preserved, neither silently wins


def test_conflict_stages_high_risk_master_untouched(make, db):
    rid, _ = make("Stage Conflict", website="pytest-stage-conflict.com", city="Munich",
                  description="widget maker")
    rid2, status2 = make("Stage Conflict", website="pytest-stage-conflict.com", city="Berlin",
                         description="widget maker")
    assert status2 == "staged_update"
    rev = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).first()
    assert rev.risk_level == "high" and "city" in (rev.proposed_changes or {})
    assert _get(db, rid).city == "Munich"  # untouched


def test_shared_domain_not_merged(make, db):
    # two different companies on the same multi-tenant domain must stay separate
    r1, s1 = make("Shared Foods", website="linkedin.com/company/pytest-a", city="Paris",
                  description="vegan meal kits delivered weekly to homes")
    r2, s2 = make("Shared Robots", website="linkedin.com/company/pytest-b", city="Tokyo",
                  description="industrial welding robots for automotive factories")
    assert r1 != r2                      # not merged
    assert s2 in ("new_master", "staged_anomaly", "staged_duplicate")
    # never silently merged into one record
    assert not (s2 == "no_op")


def test_same_name_no_website_not_silently_merged(make):
    r1, _ = make("Nova Health", city="Hamburg", description="AI logistics routing for freight")
    r2, s2 = make("Nova Health", city="Lisbon", description="artisan vegan bakery and cafe chain")
    # different companies, same name, no website -> separate ids, never a silent merge
    assert not (r2 == r1 and s2 == "no_op")


def test_possible_duplicate_between_bare_stubs_tagged_minimal_evidence(make, db):
    """
    Phase J-2 (4 Aug, review-inbox-flooding audit): a possible_duplicate
    between two records with NO description and NO website on either side
    (exactly the shape of the hochschule-biberach.de "HBC Campus 1"/"HBC
    Campus 2" incident) must be tagged evidence_level="minimal" in the
    review's evidence blob — never suppressed, just marked so the dashboard
    can filter/bulk-triage these separately from reviews with real content.
    """
    r1, s1 = make("Campus Alpha 1")
    r2, s2 = make("Campus Alpha 2")
    assert s1 == "new_master"
    assert s2 == "staged_duplicate"
    rev = db.query(DuplicateReview).filter(DuplicateReview.master_id == r1).first()
    assert rev is not None
    assert (rev.evidence or {}).get("evidence_level") == "minimal"


def test_possible_duplicate_with_real_evidence_tagged_normal(make, db):
    r1, s1 = make("Bravo Robotics 1", description="modular warehouse picking robots")
    r2, s2 = make("Bravo Robotics 2", description="modular warehouse picking robots")
    assert s2 == "staged_duplicate"
    rev = db.query(DuplicateReview).filter(DuplicateReview.master_id == r1).first()
    assert rev is not None
    assert (rev.evidence or {}).get("evidence_level") == "normal"


def test_empty_duplicate_of_a_documented_master_auto_merges(make, db):
    """
    Auto-merge exception (14 Aug 2026) — the tripbot shape: an RSS "roundup"
    article names a real, already-well-documented company with zero
    individual facts. The incoming side has nothing to contribute and the
    master is independently verified real (has a website + description), so
    this must resolve with no human involved at all — see
    processing.storage's module docstring and _is_bare_master's docstring
    for why this is safe specifically BECAUSE the master isn't bare (unlike
    test_possible_duplicate_between_bare_stubs_tagged_minimal_evidence,
    which this must NOT regress).
    """
    r1, s1 = make("Auto Merge Rich Co", website="pytest-automerge-rich.com",
                  city="Munich", industry="Software",
                  description="a real, well-documented pytest company")
    assert s1 == "new_master"

    r2, s2 = make("Auto Merge Rich Co")  # bare name-only stub, no website/description
    assert s2 == "auto_merged_empty_duplicate"
    assert r2 == r1  # folded into the SAME existing master, no surviving second row

    rev = db.query(DuplicateReview).filter(
        DuplicateReview.master_id == r1, DuplicateReview.review_type == "possible_duplicate"
    ).first()
    assert rev is None  # nothing staged — there was nothing for a human to decide

    master = _get(db, r1)
    assert master.description == "a real, well-documented pytest company"  # untouched
    assert any(h.get("url") for h in (master.source_history or []))  # but provenance grew


def test_low_confidence_empty_duplicate_still_goes_to_a_human(make, db):
    """The auto-merge exception requires risk_level=="high" specifically —
    a merely "in the review band" match with an empty incoming side must
    still be staged, not auto-resolved, since weak identity evidence plus
    nothing to independently corroborate it is exactly where a wrong guess
    could silently swallow a genuinely different company. "Foxtrot Data" vs
    "Foxtrot Analytics Base" verified empirically (14 Aug 2026) to land in
    the matcher's risk_level=="low" band (name_sim ~0.75, emb ~0.84 — below
    STRONG on name but the aggregate score still clears the review band)."""
    r1, s1 = make("Foxtrot Analytics Base", website="pytest-foxtrot-base.com",
                  city="Munich", industry="Software", description="enterprise analytics tooling")
    assert s1 == "new_master"

    r2, s2 = make("Foxtrot Data")
    assert s2 != "auto_merged_empty_duplicate"
    assert s2 == "staged_duplicate"
