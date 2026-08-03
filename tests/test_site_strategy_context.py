"""
Phase R-3 — pure formatting logic only (no Ollama call; that's verified live,
same convention as every other qwen_client method in this codebase).
"""
from reasoning.qwen_client import _site_strategy_context
from reasoning.prompts import SITE_STRATEGY_PROMPT


def _signals(**overrides):
    base = {
        "text_len": 500, "prose_density": 1.2, "link_density": 0.02,
        "jsonld_types": {}, "jsonld_item_count": 0,
        "pagination_kind": None, "render_gain": None,
        "primary_group": None, "other_groups": [],
        "detail_link_pattern": None, "detail_link_coverage": 0.0,
    }
    base.update(overrides)
    return base


def _deterministic(**overrides):
    base = {
        "page_shape": "unknown", "text_extraction": "full_text",
        "chunking": "sliding_window", "needs_render": False, "reason": "no signal",
    }
    base.update(overrides)
    return base


def test_context_renders_into_the_real_prompt_without_a_keyerror():
    group = {
        "signature": "div.card", "n": 40, "score": 0.8,
        "frac_with_link": 1.0, "frac_unique_href": 1.0, "frac_unique_name": 0.95,
        "frac_with_img": 0.9, "frac_with_heading": 0.5, "frac_headline_names": 0.1,
        "median_text_len": 20, "name_only": True,
        "sample_names": ["Acme GmbH", "Nouma Autonomy"],
    }
    ctx = _site_strategy_context(
        "https://x.test/portfolio", _signals(primary_group=group), _deterministic(page_shape="logo_grid"),
        "/portfolio",
    )
    prompt = SITE_STRATEGY_PROMPT.format(**ctx)  # raises KeyError if any placeholder is missing
    assert "Acme GmbH" in prompt
    assert "div.card" in prompt
    assert "/portfolio" in prompt


def test_context_handles_no_primary_group():
    ctx = _site_strategy_context("https://x.test/", _signals(), _deterministic(), "")
    assert "none" in ctx["group_summary"].lower()
    prompt = SITE_STRATEGY_PROMPT.format(**ctx)
    assert "(domain default)" in prompt


def test_context_handles_none_render_gain_and_pagination():
    ctx = _site_strategy_context("https://x.test/", _signals(render_gain=None, pagination_kind=None), _deterministic(), "")
    assert "not tested" in ctx["render_gain"]
    assert ctx["pagination_kind"] == "none detected"


def test_context_percentages_are_bounded_and_readable():
    group = {
        "signature": "li.x", "n": 5, "score": 0.6,
        "frac_with_link": 0.4, "frac_unique_href": 0.0, "frac_unique_name": 1.0,
        "frac_with_img": 0.0, "frac_with_heading": 1.0, "frac_headline_names": 0.0,
        "median_text_len": 10, "name_only": False, "sample_names": ["A", "B"],
    }
    ctx = _site_strategy_context("https://x.test/a", _signals(primary_group=group), _deterministic(), "/a")
    assert "0%" in ctx["group_summary"]
    assert "100%" in ctx["group_summary"]
