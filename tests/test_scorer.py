"""Deterministic scorer — tier boundaries + config-backed weights."""
from processing.scorer import tier_label, compute_enrichment_score


def test_tier_boundaries():
    assert tier_label(100) == "PRIORITY"
    assert tier_label(81) == "PRIORITY"
    assert tier_label(80) == "HIGH_QUALITY_LEAD"
    assert tier_label(61) == "HIGH_QUALITY_LEAD"
    assert tier_label(41) == "INTERESTING"
    assert tier_label(21) == "EARLY_DISCOVERY"
    assert tier_label(0) == "WEAK_SIGNAL"


def test_score_is_deterministic(db):
    from database.models import Startup
    s = db.query(Startup).filter(Startup.enrichment_score.isnot(None)).first()
    if s is None:
        return
    a = compute_enrichment_score(s)
    b = compute_enrichment_score(s)
    assert a.enrichment_score == b.enrichment_score
    assert a.score_tier == b.score_tier
    assert 0 <= a.enrichment_score <= 100
    assert a.score_tier == tier_label(a.enrichment_score)
