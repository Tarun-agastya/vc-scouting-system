"""
Phase R-4 — threading PageStrategy through fetch -> chunk -> extract.

Covers the pure-logic pieces that don't need a live LLM/network call (the
adaptive pipeline's live behaviour is verified against real sources, same
convention as every other phase in this project — see
validation/R4_VERIFICATION.md). The overriding requirement these tests
guard is the kill switch: with settings.adaptive_pipeline_enabled at its
default (False), every one of these paths must be byte-identical to the
pre-R-4 pipeline.
"""
import json

from ingestion.chunker import split_name_batches, split_cards, LOGO_GRID_CHUNK_HEADER
from ingestion.strategy import PageStrategy
from ingestion.web_scraper import extract_content, _extract_text, PageContent


# ── split_name_batches ──────────────────────────────────────────────────────

def test_split_name_batches_batches_and_headers():
    names = [f"Company {i}" for i in range(14)]
    chunks = split_name_batches(names, 6)
    assert len(chunks) == 3  # 6 + 6 + 2
    for c in chunks:
        assert c.startswith(LOGO_GRID_CHUNK_HEADER)
    assert "Company 0" in chunks[0]
    assert "Company 13" in chunks[2]


def test_split_name_batches_drops_blank_names():
    chunks = split_name_batches(["Acme", "", "  ", "Beta"], 6)
    assert len(chunks) == 1
    assert "Acme" in chunks[0] and "Beta" in chunks[0]


def test_split_name_batches_empty_input():
    assert split_name_batches([], 6) == []


# ── split_cards ──────────────────────────────────────────────────────────────

def test_split_cards_prefixes_name_and_skips_empty_text():
    blocks = [("Acme GmbH", "Acme builds widgets."), ("NoText Inc", "   "), (None, "Unnamed card body")]
    chunks = split_cards(blocks)
    assert len(chunks) == 2
    assert chunks[0].startswith("Acme GmbH:\n")
    assert "Acme builds widgets." in chunks[0]
    assert chunks[1] == "Unnamed card body"  # no name -> no prefix


# ── extract_content: full_text / DEFAULT must reproduce _extract_text exactly ──

_SAMPLE_HTML = """
<html><body>
<nav>Home | About</nav>
<main>
  <h1>Welcome</h1>
  <p>This page describes a startup called Acme Robotics.</p>
</main>
<footer>Contact us</footer>
</body></html>
"""


def test_extract_content_default_strategy_matches_legacy_extract_text():
    assert extract_content(_SAMPLE_HTML, "https://example.test/", PageStrategy.DEFAULT).text == _extract_text(_SAMPLE_HTML)


def test_extract_content_no_strategy_matches_legacy_extract_text():
    assert extract_content(_SAMPLE_HTML, "https://example.test/", None).text == _extract_text(_SAMPLE_HTML)


def test_extract_content_unrecognised_mode_falls_back_to_full_text():
    strat = PageStrategy(text_extraction="something_new")
    assert extract_content(_SAMPLE_HTML, "https://example.test/", strat).text == _extract_text(_SAMPLE_HTML)


# ── extract_content: alt_harvest / card_structured over a real card grid ────

_GRID_HTML = """
<html><body><div class="grid">
""" + "".join(
    f'<div class="card"><a href="/startups/co-{i}" title="Company {i}">Company {i}</a></div>\n'
    for i in range(6)
) + """
</div></body></html>
"""


def test_extract_content_alt_harvest_returns_structured_names_no_prose():
    strat = PageStrategy(text_extraction="alt_harvest", page_shape="logo_grid")
    content = extract_content(_GRID_HTML, "https://example.test/startups", strat)
    assert content.text == ""
    assert content.structural_count == 6
    assert sorted(content.entity_names) == sorted(f"Company {i}" for i in range(6))
    assert content.entity_blocks == []


def test_extract_content_card_structured_returns_blocks():
    strat = PageStrategy(text_extraction="card_structured", page_shape="card_directory")
    content = extract_content(_GRID_HTML, "https://example.test/startups", strat)
    assert content.structural_count == 6
    assert len(content.entity_blocks) == 6
    names, texts = zip(*content.entity_blocks)
    assert all(n and n.startswith("Company") for n in names)


def test_extract_content_card_mode_degrades_to_full_text_when_no_group_found():
    strat = PageStrategy(text_extraction="card_structured")
    content = extract_content(_SAMPLE_HTML, "https://example.test/", strat)
    assert content.text == _extract_text(_SAMPLE_HTML)
    assert content.entity_blocks == []


def test_extract_content_ignores_structural_mode_when_page_shape_is_non_content():
    """
    Regression (found live 3 Aug on zollhof.de's own homepage): a profile
    can carry text_extraction="card_structured"/"alt_harvest" while its own
    page_shape says "non_content" (e.g. an LLM-overridden verdict whose
    text_extraction/chunking came from a different pattern's high-confidence
    deterministic signal). A real card group DOES exist on the page — CTA
    buttons, not companies — and must NOT be harvested just because the
    strategy object's text_extraction field names a structural mode.
    """
    strat = PageStrategy(text_extraction="card_structured", page_shape="non_content")
    content = extract_content(_GRID_HTML, "https://example.test/startups", strat)
    assert content.text == _extract_text(_GRID_HTML)
    assert content.entity_names == []
    assert content.entity_blocks == []


def test_extract_content_alt_harvest_ignored_when_page_shape_is_article_feed():
    strat = PageStrategy(text_extraction="alt_harvest", page_shape="article_feed")
    content = extract_content(_GRID_HTML, "https://example.test/news", strat)
    assert content.entity_names == []


# ── extract_content: main_prose falls back cleanly on empty trafilatura output ─

def test_extract_content_main_prose_falls_back_when_trafilatura_finds_nothing():
    strat = PageStrategy(text_extraction="main_prose")
    # A near-empty shell trafilatura can't extract anything meaningful from.
    content = extract_content("<html><body></body></html>", "https://example.test/", strat)
    assert content.text == _extract_text("<html><body></body></html>")


# ── qwen_client.extract_startups: prompt selection by chunk_kind ────────────

class _FakeOllamaClient:
    """Captures the prompt sent instead of calling a real Ollama server."""

    def __init__(self, response_startups=None):
        self.last_messages = None
        self._response_startups = response_startups if response_startups is not None else []

    def chat(self, *, model, messages, format, options):
        self.last_messages = messages
        return {"message": {"content": json.dumps({"startups": self._response_startups})}}


def _extract_with_fake_client(chunk_kind):
    from reasoning.qwen_client import QwenClient

    client = QwenClient()
    fake = _FakeOllamaClient()
    client._extract_ollama_client = fake  # bypass lazy ollama.Client() construction
    client.extract_startups("Some chunk text about a company.", chunk_kind=chunk_kind)
    return fake.last_messages[1]["content"]  # the user-role prompt


def test_extract_startups_default_chunk_kind_uses_full_prompt_with_bare_name_lists():
    prompt = _extract_with_fake_client(None)
    assert "BARE NAME LISTS" in prompt


def test_extract_startups_name_batch_chunk_kind_uses_full_prompt():
    prompt = _extract_with_fake_client("name_batch")
    assert "BARE NAME LISTS" in prompt


def test_extract_startups_prose_chunk_kind_uses_trimmed_prompt():
    prompt = _extract_with_fake_client("prose")
    assert "BARE NAME LISTS" not in prompt


def test_extract_startups_card_chunk_kind_uses_trimmed_prompt():
    prompt = _extract_with_fake_client("card")
    assert "BARE NAME LISTS" not in prompt
