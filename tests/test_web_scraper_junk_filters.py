"""
Regression tests for the 4 Aug incident: a hochschule-biberach.de crawl
wandered into off-topic institutional pages (professor recruitment,
university governance, cafeteria, IT department, "our network" partner
list) and their photo captions / established-firm logos got written to the
DB as ~124 fake "startups" (e.g. "HBC Campus 1".."HBC Campus 5", "HBC
Keyfact 4".."HBC Keyfact 9"), which then flooded the Review Inbox with
possible_duplicate pairs between the junk records themselves.

Two independent, additive bugs, each covered here:
1. _is_irrelevant_url only matched whole path segments against
   SKIP_PATTERNS, so a hyphenated compound like "/prof-karriere" slipped
   past the existing "karriere" skip entry.
2. The alt-text harvest had no way to tell a genuine logo grid (distinct
   company names) from a photo gallery/infographic (a shared caption
   prefix with an incrementing slide number) — both look identical to a
   per-entry check.
"""
from ingestion.web_scraper import _is_irrelevant_url, _drop_numbered_sequences


def test_skip_matches_hyphenated_sub_token():
    # "karriere" is in SKIP_PATTERNS but the real crawled segment was the
    # compound "prof-karriere" — must still be caught.
    assert _is_irrelevant_url("https://www.hochschule-biberach.de/prof-karriere")


def test_skip_does_not_false_positive_on_substring():
    # "research" must NOT be skipped just because it contains "search"
    # (a real SKIP_PATTERNS entry) — only real sub-tokens count.
    assert not _is_irrelevant_url("https://example.com/research")
    assert not _is_irrelevant_url("https://example.com/online-platform")


def test_skip_whole_segment_still_works():
    assert _is_irrelevant_url("https://example.com/login")
    assert not _is_irrelevant_url("https://example.com/portfolio/founders")


def test_university_housekeeping_segments_skipped():
    assert _is_irrelevant_url("https://www.hochschule-biberach.de/hochschule/organe")
    assert _is_irrelevant_url(
        "https://www.hochschule-biberach.de/hochschule/einrichtungen/mensa-tiny-diny"
    )
    assert _is_irrelevant_url(
        "https://www.hochschule-biberach.de/hochschule/einrichtungen/rechenzentrum"
    )


def test_drop_numbered_sequences_removes_gallery_captions():
    alts = [
        "HBC Campus 1", "HBC Campus 2", "HBC Campus 3",
        "HBC Keyfact 4", "HBC Keyfact 5", "HBC Keyfact 6",
        "Zollhof", "Sinjection", "Beamler",
    ]
    result = _drop_numbered_sequences(alts)
    assert result == ["Zollhof", "Sinjection", "Beamler"]


def test_drop_numbered_sequences_keeps_real_startups_ending_in_digits():
    # A real company name that happens to end in a digit, appearing once,
    # must never be dropped just for matching the trailing-number shape.
    alts = ["4Screen", "Bloom42", "Traefik2"]
    assert _drop_numbered_sequences(alts) == alts


def test_drop_numbered_sequences_ignores_non_numbered_alts():
    alts = ["Reisacher", "Kutter", "SÜDPACK"]
    assert _drop_numbered_sequences(alts) == alts


def test_degree_programme_catalogue_pages_are_not_crawled():
    """
    Confirmed junk twice, 16 + 17 Sep 2026. A university curriculum page lists
    subject areas and the professor teaching each, which the extractor reads as
    a company plus a website — hochschule-biberach.de/studium/bachelorstudium/
    architektur produced "Konstruktion und Entwerfen" and "Geschichte und
    Theorie der Architektur" as startups, each with a /kontakt/<person> staff
    page as its "website".

    Deleted on the 16th and back within a day: repointing the source's entry
    URL doesn't stop the crawl frontier reaching these pages from the new
    entry point, so the block has to be on the page.

    Blocked by URL rather than by name on purpose — every name-shaped rule
    broad enough to catch "Konstruktion und Entwerfen" also catches plausible
    real company names, and the junk gate drops the whole record.
    """
    from ingestion.web_scraper import _is_irrelevant_url

    for url in (
        "https://www.hochschule-biberach.de/studium/bachelorstudium/architektur",
        "https://www.hochschule-biberach.de/studium/bachelorstudium/betriebswirtschaftslehre",
        "https://www.tha.de/studium/studienangebot/bachelorstudiengaenge",
        "https://www.tha.de/studium/studienangebot/masterstudiengaenge",
        "https://example.de/studium/modulhandbuch",
    ):
        assert _is_irrelevant_url(url), f"{url} is a course catalogue and must not be crawled"


def test_the_pages_that_actually_produce_startups_are_still_crawled():
    """
    The other half, and the reason the new patterns are narrow: `studium` is
    deliberately NOT blocked, because it is the parent of the catalogue pages
    above but can also hold a genuine Gründung page. Verified against every
    source URL in the live database when shipped — exactly one was newly
    blocked, and it was the junk one.
    """
    from ingestion.web_scraper import _is_irrelevant_url

    for url in (
        "https://www.hochschule-biberach.de/gruenderinitiative",
        "https://accelerator.unternehmertum.de/portfolio",
        "https://www.tha.de/hochschule/organisation/einrichtungen/"
        "institut-fuer-gruendung-und-innovation-tha-funkenwerk/inkubatorteams/",
        "https://startupsucht.com/startup-liste-verzeichnis-dresden",
        "https://www.munich-startup.de/startups/",
        "https://example.de/studium/gruendung",
    ):
        assert not _is_irrelevant_url(url), f"{url} produces real companies and must stay crawlable"
