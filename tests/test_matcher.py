"""build_match_report classification (needs DB + Qdrant + embeddings)."""
from processing.matcher import (
    build_match_report, _identity_domain, _classify, _text_overlap,
)
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


def test_classify_same_city_alone_does_not_trigger_review():
    """
    Regression (4 Aug, review-inbox flooding audit): two unrelated startups
    that happen to share a city, plus a moderately-strong embedding score
    from generic same-industry vocabulary overlap (not real identity
    evidence), must NOT be enough to stage a possible_duplicate review —
    name is genuinely different and there's no founder overlap.
    """
    ev = {"name_similarity": 0.3, "embedding_sim": 0.85, "founder_overlap": 0.0,
          "location_match": 1.0, "aggregate_score": 0.4}
    outcome, _, _ = _classify(ev, domain_match=False)
    assert outcome == "no_match"


def test_classify_strong_name_and_embedding_no_longer_need_location():
    """
    Part A (4 Aug): location was dropped from the strong-signal rule
    entirely. name AND embedding both independently clearing the strong bar
    is sufficient evidence on its own — a same-company pair whose city
    happens to be missing/different must still be flagged.
    """
    ev = {"name_similarity": 0.9, "embedding_sim": 0.9, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.6, "text_overlap": 0.4}
    outcome, risk, _ = _classify(ev, domain_match=False)
    assert outcome == "possible_duplicate" and risk == "high"


def test_classify_text_overlap_veto_kills_embedding_only_false_positive():
    """
    Part B (4 Aug): the real shape measured on the live pending backlog —
    "Vaeridion ~ Bai Soft GmbH": different names, generically-elevated
    embedding (same-industry vocabulary), and ZERO literal description
    overlap. 72% of comparable pending possible_duplicate reviews looked
    like this. Literal-text disagreement is decisive; the embedding is not.
    """
    ev = {"name_similarity": 0.353, "embedding_sim": 0.782, "founder_overlap": 0.0,
          "location_match": 1.0, "aggregate_score": 0.5, "text_overlap": 0.0}
    outcome, _, reason = _classify(ev, domain_match=False)
    assert outcome == "no_match"
    assert "literal" in reason


def test_classify_veto_does_not_fire_when_names_are_similar():
    """
    The veto needs BOTH conditions. A genuine duplicate re-scraped with a
    rewritten description (high name similarity, low literal overlap) must
    survive — otherwise re-worded copy would silently hide real duplicates.
    """
    ev = {"name_similarity": 0.95, "embedding_sim": 0.9, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.6, "text_overlap": 0.0}
    outcome, _, _ = _classify(ev, domain_match=False)
    assert outcome == "possible_duplicate"


def test_classify_veto_never_fires_without_descriptions():
    """
    text_overlap is None when either record has no description — absence of
    text is absence of evidence, NOT evidence of difference. Vetoing here
    would break Phase D-1's same-name-no-website duplicate detection, whose
    records are bare name-only stubs by definition.
    """
    ev = {"name_similarity": 0.95, "embedding_sim": 0.95, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.6, "text_overlap": None}
    outcome, _, _ = _classify(ev, domain_match=False)
    assert outcome == "possible_duplicate"


def test_classify_veto_does_not_apply_to_domain_matches():
    """A real shared identity-domain outranks any text signal."""
    ev = {"name_similarity": 0.1, "embedding_sim": 0.9, "founder_overlap": 0.0,
          "location_match": 0.0, "aggregate_score": 0.4, "text_overlap": 0.0}
    outcome, _, _ = _classify(ev, domain_match=True)
    assert outcome in ("possible_duplicate", "anomaly")


def test_text_overlap_function():
    text = "builds ai powered logistics routing software for freight forwarders"
    assert _text_overlap(text, text) == 1.0
    assert _text_overlap(text, "vegan meal kits delivered weekly to homes") == 0.0
    # None (not 0.0) whenever either side is missing/too short to shingle
    assert _text_overlap(text, "") is None
    assert _text_overlap(None, text) is None
    assert _text_overlap("two words", text) is None  # < 3 words -> no shingle


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
