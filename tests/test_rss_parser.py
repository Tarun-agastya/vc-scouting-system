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
