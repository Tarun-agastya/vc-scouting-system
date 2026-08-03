"""
Phase R-6 — detail-page following.

Covers the pure logic (_matches_detail_pattern, extract_content's
entity_links, entity_hint propagation through the chunker) without a live
LLM/network call. The end-to-end crawl behaviour (a real detail-link frontier
tier, budget capping, Company: prefix reaching a real extraction) is
verified live — see validation/R6_VERIFICATION.md.
"""
import asyncio

from ingestion.web_scraper import _matches_detail_pattern, extract_content
from ingestion.strategy import PageStrategy
from ingestion.worker_queue import PageItem, ChunkItem, PipelineMetrics, chunker_task


# ── _matches_detail_pattern ─────────────────────────────────────────────────

def test_matches_detail_pattern_real_subpath():
    assert _matches_detail_pattern("https://x.test/startupdate/acme-gmbh", "/startupdate/*")


def test_matches_detail_pattern_rejects_the_bare_prefix_itself():
    assert not _matches_detail_pattern("https://x.test/startupdate", "/startupdate/*")
    assert not _matches_detail_pattern("https://x.test/startupdate/", "/startupdate/*")


def test_matches_detail_pattern_rejects_unrelated_path():
    assert not _matches_detail_pattern("https://x.test/about", "/startupdate/*")


def test_matches_detail_pattern_rejects_pattern_without_wildcard_suffix():
    assert not _matches_detail_pattern("https://x.test/startupdate/acme", "/startupdate")


def test_matches_detail_pattern_none_pattern():
    assert not _matches_detail_pattern("https://x.test/startupdate/acme", None)


def test_matches_detail_pattern_prefix_must_match_exactly_not_just_substring():
    # "/startupdate-archive/x" should NOT match "/startupdate/*" — the
    # prefix comparison includes the trailing slash, so a longer sibling
    # segment name can't accidentally satisfy startswith().
    assert not _matches_detail_pattern("https://x.test/startupdate-archive/x", "/startupdate/*")


# ── extract_content: entity_links ───────────────────────────────────────────

_GRID_WITH_LINKS_HTML = """
<html><body><div class="grid">
""" + "".join(
    f'<div class="card"><a href="/startupdate/co-{i}" title="Company {i}">Company {i}</a></div>\n'
    for i in range(6)
) + """
</div></body></html>
"""


def test_extract_content_alt_harvest_returns_entity_links():
    strat = PageStrategy(text_extraction="alt_harvest", page_shape="logo_grid")
    content = extract_content(_GRID_WITH_LINKS_HTML, "https://x.test/", strat)
    assert len(content.entity_links) == 6
    names = sorted(n for n, _ in content.entity_links)
    assert names == [f"Company {i}" for i in range(6)]
    for name, href in content.entity_links:
        assert href.startswith("https://x.test/startupdate/co-")


def test_extract_content_card_structured_also_returns_entity_links():
    strat = PageStrategy(text_extraction="card_structured", page_shape="card_directory")
    content = extract_content(_GRID_WITH_LINKS_HTML, "https://x.test/", strat)
    assert len(content.entity_links) == 6


def test_extract_content_entity_links_empty_when_no_primary_group():
    strat = PageStrategy(text_extraction="alt_harvest", page_shape="logo_grid")
    content = extract_content("<html><body><p>nothing here</p></body></html>", "https://x.test/", strat)
    assert content.entity_links == []


def test_extract_content_main_prose_mode_has_no_entity_links():
    strat = PageStrategy(text_extraction="main_prose")
    content = extract_content(_GRID_WITH_LINKS_HTML, "https://x.test/", strat)
    assert content.entity_links == []


# ── entity_hint propagation through the chunker ─────────────────────────────

def test_chunker_propagates_parent_entity_name_as_entity_hint():
    async def run():
        page_q, chunk_q = asyncio.Queue(), asyncio.Queue()
        item = PageItem(
            url="https://x.test/startupdate/acme",
            text=(
                "Acme GmbH is a startup founded in Munich that raised a Series A "
                "funding round to build its B2B SaaS platform for logistics companies "
                "across Germany, backed by several venture capital investors."
            ),
            source_type="test", source_url="https://x.test/", parent_entity_name="Acme GmbH",
        )
        await page_q.put(item)
        await page_q.put(None)
        metrics = PipelineMetrics()
        await chunker_task(page_q, chunk_q, metrics)

        chunks = []
        while not chunk_q.empty():
            c = chunk_q.get_nowait()
            if c is not None:
                chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert chunks, "expected at least one chunk from a real-length page"
    assert all(c.entity_hint == "Acme GmbH" for c in chunks)


def test_chunker_entity_hint_none_when_no_parent():
    async def run():
        page_q, chunk_q = asyncio.Queue(), asyncio.Queue()
        item = PageItem(
            url="https://x.test/",
            text=(
                "Acme GmbH is a startup founded in Munich that raised a Series A "
                "funding round to build its B2B SaaS platform for logistics companies "
                "across Germany, backed by several venture capital investors."
            ),
            source_type="test", source_url="https://x.test/",
        )
        await page_q.put(item)
        await page_q.put(None)
        metrics = PipelineMetrics()
        await chunker_task(page_q, chunk_q, metrics)
        chunks = []
        while not chunk_q.empty():
            c = chunk_q.get_nowait()
            if c is not None:
                chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert chunks
    assert all(c.entity_hint is None for c in chunks)
