"""
ingestion/rss_parser.py::ingest_feeds() -- regression for a dead-code bug
found live 12 Aug 2026: the function's closing `logger.info` + `return
all_startups` were stranded after an unrelated method (_get_published_date)
inserted between them and the loop that builds all_startups, so
ingest_feeds() always implicitly returned None. Storage itself was
unaffected (_store_startup calls upsert_startup per-entry, inside the loop),
but every caller of the return value -- specifically
processing/scout_controller.py::_work_rss -- had nothing real to report,
which is why the Ingestion Control dashboard showed a flat 0/"-" for every
RSS run, both while running and after it finished.
"""
from unittest.mock import patch

from ingestion.rss_parser import RSSParser


def test_ingest_feeds_returns_the_extracted_startups_not_none():
    parser = RSSParser()
    fake_entry = type("FakeEntry", (), {"link": "https://example.com/a", "title": "A"})()
    fake_feed = type("FakeFeed", (), {"entries": [fake_entry]})()

    with patch("ingestion.rss_parser.feedparser.parse", return_value=fake_feed), \
         patch.object(parser, "_process_entry", return_value=[{"name": "PYTEST Fake Startup"}]):
        result = parser.ingest_feeds(feed_urls=["https://example.com/feed"], max_entries=5)

    assert result is not None, "ingest_feeds() must return the extracted list, not implicitly None"
    assert result == [{"name": "PYTEST Fake Startup"}]


def test_ingest_feeds_returns_empty_list_not_none_when_nothing_found():
    parser = RSSParser()
    fake_feed = type("FakeFeed", (), {"entries": []})()

    with patch("ingestion.rss_parser.feedparser.parse", return_value=fake_feed):
        result = parser.ingest_feeds(feed_urls=["https://example.com/feed"], max_entries=5)

    assert result == []


def test_ingest_feeds_survives_one_feed_failing():
    """A feed that raises must not abort the whole run -- entries from
    other feeds already collected must still come back."""
    parser = RSSParser()
    fake_entry = type("FakeEntry", (), {"link": "https://example.com/b", "title": "B"})()
    fake_feed = type("FakeFeed", (), {"entries": [fake_entry]})()

    def parse_side_effect(url):
        if url == "https://bad.example.com/feed":
            raise ValueError("boom")
        return fake_feed

    with patch("ingestion.rss_parser.feedparser.parse", side_effect=parse_side_effect), \
         patch.object(parser, "_process_entry", return_value=[{"name": "PYTEST Survivor"}]):
        result = parser.ingest_feeds(
            feed_urls=["https://bad.example.com/feed", "https://example.com/feed"], max_entries=5)

    assert result == [{"name": "PYTEST Survivor"}]


def test_store_startup_gives_upsert_the_article_url_and_feed_as_origin():
    """Regression for a feed-URL/source-label swap found live 13 Aug 2026:
    _store_startup used to pass the RSS feed URL as `source` (so every
    RSS-derived record's source_entry["source"] was a raw feed URL instead
    of a clean label) and the specific article link as `source_url` for
    registry name-resolution -- which can never match a feed's registered
    URL, so _resolve_source_name() silently returned None for every single
    RSS record. Correct contract (mirrors newsletter_ingestor's
    source="newsletter" and web_scraper's Phase R-4 origin_url pattern):
    source="rss" (clean label), source_url=the article (accurate citation),
    origin_url=the feed (so registry name-lookup matches the feed entry)."""
    parser = RSSParser()

    with patch("processing.storage.upsert_startup", return_value=("fake-id", "inserted")) as mock_upsert:
        parser._store_startup(
            {"name": "PYTEST Startup"},
            "https://example.com/articles/pytest-startup-raises",
            "https://example.com/feed",
            "2026-08-13T00:00:00",
        )

    mock_upsert.assert_called_once_with(
        {"name": "PYTEST Startup"},
        "rss",
        "https://example.com/articles/pytest-startup-raises",
        "2026-08-13T00:00:00",
        origin_url="https://example.com/feed",
    )


def test_process_entry_passes_feed_url_as_origin_not_as_source_url():
    """End-to-end wiring check: the feed URL must reach _store_startup as
    feed_url (-> origin_url), never as source_url, so a multi-article feed's
    records each cite their own article rather than all pointing at the
    feed itself."""
    parser = RSSParser()
    fake_entry = type(
        "FakeEntry", (),
        {"link": "https://example.com/articles/real-one", "title": "T", "summary": "S" * 100},
    )()

    calls = []

    def fake_store(startup, source_url, feed_url, published_date=None):
        calls.append((source_url, feed_url))

    with patch("ingestion.pipeline.pipeline.run", return_value=[{"name": "PYTEST Wired"}]), \
         patch.object(parser, "_store_startup", side_effect=fake_store):
        parser._process_entry(fake_entry, "https://example.com/feed")

    assert calls == [("https://example.com/articles/real-one", "https://example.com/feed")]
