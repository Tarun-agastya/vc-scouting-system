"""build_match_report classification (needs DB + Qdrant + embeddings)."""
from processing.matcher import build_match_report, _identity_domain, _classify
from embeddings.embedder import embedder


def _vec(startup):
    return embedder.embed(embedder.build_startup_text(startup))


def test_identity_domain_blocklist():
    assert _identity_domain("https://linkedin.com/company/x") is None  # multi-tenant
    assert _identity_domain("https://medium.com/@x") is None
    assert _identity_domain("https://acme.io/about") == "acme.io"
    assert _identity_domain("") is None


def test_classify_shared_domain_anomaly():
    ev = {"name_similarity": 0.1, "embedding_sim": 0.2, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.15}
    outcome, risk, _ = _classify(ev, domain_match=True)
    assert outcome == "anomaly"


def test_classify_domain_rename():
    ev = {"name_similarity": 0.4, "embedding_sim": 0.9, "founder_overlap": 0.0,
          "location_match": 1.0, "aggregate_score": 0.55}
    outcome, risk, _ = _classify(ev, domain_match=True)
    assert outcome == "possible_duplicate" and risk == "high"


def test_classify_weak_no_match():
    ev = {"name_similarity": 0.3, "embedding_sim": 0.4, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.25}
    outcome, _, _ = _classify(ev, domain_match=False)
    assert outcome == "no_match"


def test_exact_same_record(make, db):
    make("Matcher Exact", website="pytest-matcher-exact.com", city="Munich",
         description="robotics for warehouses")
    incoming = {"name": "PYTEST Matcher Exact", "website": "pytest-matcher-exact.com",
                "city": "Munich", "description": "robotics for warehouses"}
    rep = build_match_report(incoming, db, _vec(incoming))
    assert rep.outcome == "exact_same_record"


def test_rename_same_domain_is_possible_duplicate(make, db):
    make("Matcher Rename", website="pytest-matcher-rename.com", city="Berlin",
         description="AI logistics routing")
    incoming = {"name": "PYTEST Rename Mobility GmbH", "website": "pytest-matcher-rename.com",
                "city": "Berlin", "description": "AI logistics routing"}
    rep = build_match_report(incoming, db, _vec(incoming))
    # different name, same real domain -> flagged, never auto-linked
    assert rep.outcome in ("possible_duplicate", "exact_same_record")
    assert rep.master_id is not None


def test_brand_new_no_match(make, db):
    incoming = {"name": "PYTEST Totally Unique Zqxw", "website": "pytest-uniq-zqxw.com",
                "city": "Reykjavik", "description": "volcanic geothermal drilling rigs"}
    rep = build_match_report(incoming, db, _vec(incoming))
    assert rep.outcome == "no_match"
