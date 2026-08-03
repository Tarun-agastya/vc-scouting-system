"""
Phase R-7 — get_profile's strict-mode domain-default-fallback fix.

Two real bugs (R-6: detail pages, R-7: ordinary subpages) both traced back
to the SAME root cause: a page lacking its own profile silently inherited
its domain's default strategy, which can be for a structurally unrelated
page. strict=True disables the fallback; the domain-default page's own
exact match (pattern == "") must still work even under strict=True, since
that's an exact match, not a fallback.
"""
import uuid

import pytest

from database.connection import SessionLocal
from database.models import SiteProfile
from ingestion.strategy import PageStrategy
from processing.site_profile_store import get_profile, _apply_strategy


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _cleanup(db, domain):
    db.query(SiteProfile).filter(SiteProfile.domain == domain).delete()
    db.commit()


def _make_domain_default(db, domain):
    row = SiteProfile(domain=domain, url_pattern="")
    strat = PageStrategy(page_shape="article_feed", text_extraction="main_prose",
                          chunking="sliding_window", confidence="high", source="llm")
    _apply_strategy(row, strat, source_id=None, signals={"text_len": 1})
    db.add(row)
    db.commit()
    return row


def test_strict_true_does_not_fall_back_to_domain_default(db):
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        _make_domain_default(db, domain)
        hit = get_profile(f"https://{domain}/portfolio", strict=True)
        assert hit is None  # no exact-pattern row for /portfolio -> no fallback under strict
    finally:
        _cleanup(db, domain)


def test_strict_false_still_falls_back_to_domain_default(db):
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        _make_domain_default(db, domain)
        hit = get_profile(f"https://{domain}/portfolio", strict=False)
        assert hit is not None
        assert hit.url_pattern == ""
    finally:
        _cleanup(db, domain)


def test_strict_true_still_matches_the_domain_default_page_itself(db):
    """pattern == "" (the domain-default page's own URL) is an EXACT match
    against its own row, never a fallback — must work even under strict."""
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        _make_domain_default(db, domain)
        hit = get_profile(f"https://{domain}/", strict=True)
        assert hit is not None
        assert hit.url_pattern == ""
    finally:
        _cleanup(db, domain)


def test_strict_true_still_matches_an_exact_pattern_profile(db):
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        _make_domain_default(db, domain)
        specific = SiteProfile(domain=domain, url_pattern="/portfolio")
        strat = PageStrategy(page_shape="card_directory", text_extraction="card_structured",
                              chunking="per_card", confidence="high", source="deterministic")
        _apply_strategy(specific, strat, source_id=None, signals={"text_len": 1})
        db.add(specific)
        db.commit()

        hit = get_profile(f"https://{domain}/portfolio", strict=True)
        assert hit is not None
        assert hit.url_pattern == "/portfolio"
        assert hit.page_shape == "card_directory"
    finally:
        _cleanup(db, domain)
