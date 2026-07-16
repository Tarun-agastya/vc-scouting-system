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


def test_blank_fill_stages_low_risk(make, db):
    rid, _ = make("Stage Blank", website="pytest-stage-blank.com", city="Munich",
                  description="widget maker")
    rid2, status2 = make("Stage Blank", website="pytest-stage-blank.com", city="Munich",
                         description="widget maker", funding_stage="Seed")
    assert status2 == "staged_update" and rid2 == rid
    rev = db.query(DuplicateReview).filter(DuplicateReview.master_id == rid,
                                           DuplicateReview.review_type == "field_update").first()
    assert rev and rev.risk_level == "low" and "funding_stage" in (rev.proposed_changes or {})
    # master itself is NOT modified
    assert _get(db, rid).funding_stage is None


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
