"""
Phase R-2/R-3 — processing/site_profile_store.py, the parts testable without
a live LLM/network call (probe_and_store's own end-to-end behaviour is
verified live, same convention as every other qwen_client-calling path in
this codebase).
"""
import uuid

import pytest

from database.connection import SessionLocal
from database.models import SiteProfile
from ingestion.strategy import PageStrategy
from processing.site_profile_store import (
    normalize_domain, normalize_path_pattern, _apply_strategy, _store_speculative_profile,
)


def test_normalize_domain_strips_www_and_lowercases():
    assert normalize_domain("https://WWW.Example.com/x") == "example.com"
    assert normalize_domain("https://example.com/x") == "example.com"


def test_normalize_path_pattern_collapses_detail_slugs_only():
    assert normalize_path_pattern("https://x.test/startups/acme-gmbh") == "/startups/*"
    assert normalize_path_pattern("https://x.test/news-details/story-42") == "/news-details/*"
    assert normalize_path_pattern("https://x.test/companies/123") == "/companies/*"
    # A listing page's own path has no slug-like trailing segment — must NOT collapse.
    assert normalize_path_pattern("https://x.test/startup-incubation/portfolio") == "/startup-incubation/portfolio"
    assert normalize_path_pattern("https://x.test/") == ""
    assert normalize_path_pattern("https://x.test") == ""


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _cleanup(db, domain):
    db.query(SiteProfile).filter(SiteProfile.domain == domain).delete()
    db.commit()


def test_row_survives_speculative_profile_storage_without_detaching(db):
    """
    Regression (found live 3 Aug, testing against schwaben.digital's
    detail_link_pattern): _store_speculative_profile does its OWN commit,
    which — via SQLAlchemy's default expire_on_commit=True — expires every
    object already loaded in the session, including an already-refreshed
    `row`. Calling it AFTER row's final refresh left row expired-and-then-
    detached once the caller's session closed, raising
    DetachedInstanceError on the very first attribute access. Fixed by
    storing speculative profiles BEFORE the final refresh, not after.
    """
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        row = SiteProfile(domain=domain, url_pattern="")
        db.add(row)
        strat = PageStrategy(page_shape="article_feed", confidence="medium", source="llm")
        _apply_strategy(row, strat, source_id=None, signals={"text_len": 1})
        db.commit()
        db.refresh(row)

        # Simulate what probe_and_store does when the LLM returns a second,
        # speculative profile for a detail-link pattern.
        extra = ("/events/*", PageStrategy(page_shape="detail_page", source="llm"))
        _store_speculative_profile(db, domain, extra)

        # This is the exact ordering probe_and_store now uses: refresh AFTER
        # the speculative writes, so `row` is valid right up to session close.
        db.refresh(row)

        assert row.page_shape == "article_feed"  # no DetachedInstanceError raised

        speculative = db.query(SiteProfile).filter_by(domain=domain, url_pattern="/events/*").first()
        assert speculative is not None
        assert speculative.page_shape == "detail_page"
        assert speculative.probed_at is None  # never actually probed — the whole point of "speculative"
    finally:
        _cleanup(db, domain)


def test_speculative_profile_never_overwrites_a_real_one(db):
    """A genuine probe always outranks a speculative guess for the same pattern."""
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        real = SiteProfile(domain=domain, url_pattern="/events/*")
        strat = PageStrategy(page_shape="card_directory", confidence="high", source="deterministic")
        _apply_strategy(real, strat, source_id=None, signals={"text_len": 1})
        db.add(real)
        db.commit()

        _store_speculative_profile(db, domain, ("/events/*", PageStrategy(page_shape="detail_page", source="llm")))

        row = db.query(SiteProfile).filter_by(domain=domain, url_pattern="/events/*").first()
        assert row.page_shape == "card_directory"  # untouched
        assert row.strategy_source == "deterministic"
    finally:
        _cleanup(db, domain)


def test_flagged_profile_is_reported_as_due_for_reprobe():
    """
    16 Sep 2026. record_recall_outcome flags a profile after two consecutive
    shortfalls, and _retry_recall_shortfalls' docstring promises that this
    "forces a fresh probe next time". needs_reprobe() implements that — but it
    was only ever called inside probe_and_store(), which nothing but the
    dashboard's manual Re-inspect reaches, so the crawl path used whatever
    get_profile() returned and reused a failing strategy indefinitely.

    Found live in the profile store: hochschule-biberach's
    /studium/bachelorstudium/betriebswirtschaftslehre at status=flagged,
    recall=0.0, still being handed out, plus six more on tha.de.
    """
    from database.models import SiteProfile
    from processing.site_profile_store import needs_reprobe

    flagged = SiteProfile(domain="pytest-reprobe.test", url_pattern="/listing",
                          status="flagged", consecutive_shortfalls=2)
    assert needs_reprobe(flagged), "a twice-failed profile must be due for a fresh probe"

    pinned = SiteProfile(domain="pytest-reprobe.test", url_pattern="/listing",
                         status="pinned", consecutive_shortfalls=5)
    assert not needs_reprobe(pinned), "a human's pin must never expire"


def test_crawler_consults_needs_reprobe_before_trusting_a_cached_profile():
    """
    The wiring itself, pinned. A cached profile is only usable if
    needs_reprobe() says so — otherwise the flag set by the recall audit is
    decoration. Asserted against the source because the alternative is a full
    live crawl, and what matters is that the call exists on this path at all.
    """
    import inspect
    from ingestion import web_scraper

    src = inspect.getsource(web_scraper.WebScraper)
    assert "needs_reprobe(known_profile)" in src, (
        "the crawl path no longer checks needs_reprobe() — a profile that "
        "failed its recall audit twice will be reused forever"
    )
    # and the discard must route into the free fresh-probe path, not an error
    assert "known_profile = None" in src
