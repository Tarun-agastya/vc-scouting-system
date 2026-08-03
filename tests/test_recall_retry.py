"""
Phase R-5 — recall audit + auto-retry ladder.

Covers the pure decision logic (PipelineMetrics.shortfall_pages,
next_retry_step) without a live LLM/network call, plus a DB round-trip for
apply_retry_result (same convention as tests/test_site_profile_store.py).
The end-to-end retry behaviour (actual recovery on a real source) is
verified live — see validation/R5_VERIFICATION.md.
"""
import uuid
from types import SimpleNamespace

import pytest

from database.connection import SessionLocal
from database.models import SiteProfile
from ingestion.worker_queue import PipelineMetrics, PageOutcome
from processing.site_profile_store import next_retry_step, apply_retry_result, _apply_strategy
from ingestion.strategy import PageStrategy


# ── PipelineMetrics.shortfall_pages ─────────────────────────────────────────

def test_shortfall_requires_expectation_ratio_and_gap():
    m = PipelineMetrics()
    m.record_expectation("https://x.test/a", 10, shape="logo_grid")
    for name in ("n1", "n2", "n3"):  # 3/10 = 0.3 recall, gap 7 — a real shortfall
        m.record_extraction("https://x.test/a", name)
    assert [o.url for o in m.shortfall_pages(ratio=0.6, min_gap=5)] == ["https://x.test/a"]


def test_shortfall_gap_floor_stops_thrash_on_small_pages():
    m = PipelineMetrics()
    m.record_expectation("https://x.test/small", 3, shape="logo_grid")
    m.record_extraction("https://x.test/small", "n1")  # 1/3 = 0.33 recall, but gap is only 2
    assert m.shortfall_pages(ratio=0.6, min_gap=5) == []


def test_no_expectation_never_qualifies_as_shortfall():
    m = PipelineMetrics()
    m.record_extraction("https://x.test/prose", "some name")  # no record_expectation call
    assert m.shortfall_pages(ratio=0.6, min_gap=5) == []


def test_shortfall_pages_sorted_worst_gap_first():
    m = PipelineMetrics()
    m.record_expectation("https://x.test/worse", 20, shape="logo_grid")
    m.record_expectation("https://x.test/less-worse", 10, shape="logo_grid")
    for i in range(2):
        m.record_extraction("https://x.test/worse", f"n{i}")       # gap 18
        m.record_extraction("https://x.test/less-worse", f"n{i}")  # gap 8
    urls = [o.url for o in m.shortfall_pages(ratio=0.6, min_gap=5)]
    assert urls == ["https://x.test/worse", "https://x.test/less-worse"]


# ── next_retry_step — the ladder itself ─────────────────────────────────────

def _fake_profile(**overrides) -> SimpleNamespace:
    base = dict(
        id="fake-id", page_shape="logo_grid", text_extraction="alt_harvest",
        chunking_mode="name_batch", needs_render=False, paginate=False,
        follow_detail_links=False, detail_link_pattern=None,
        bypass_candidate_filter=True, names_per_chunk=None, load_more_selector=None,
        max_pages=None, max_depth=None, max_load_more=None,
        expected_entity_count=100, confidence="high", reason="",
        strategy_source="deterministic", retry_ladder_position=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_step1_qwen_failures_halves_names_per_chunk():
    profile = _fake_profile(names_per_chunk=12)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=2)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 1
    assert strat.names_per_chunk == 6


def test_step1_default_batch_size_when_none_set():
    profile = _fake_profile(names_per_chunk=None)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=1)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 1
    assert strat.names_per_chunk == 3  # half of the config default (6)


def test_step2_not_rendered_escalates_to_render_and_paginate():
    profile = _fake_profile(needs_render=False)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 2
    assert strat.needs_render is True
    assert strat.paginate is True


def test_step3_rendered_not_paginated_enables_pagination():
    profile = _fake_profile(needs_render=True, paginate=False)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 3
    assert strat.paginate is True


def test_step4_pagination_hit_cap_doubles_max_load_more():
    profile = _fake_profile(needs_render=True, paginate=True, max_load_more=15)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    metrics = PipelineMetrics()
    metrics.pagination_hit_cap.add("https://x.test/a")
    idx, strat = next_retry_step(profile, outcome, metrics)
    assert idx == 4
    assert strat.max_load_more == 30


def test_step5_card_structured_falls_back_to_alt_harvest():
    profile = _fake_profile(needs_render=True, paginate=True, text_extraction="card_structured")
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 5
    assert strat.text_extraction == "alt_harvest"
    assert strat.chunking == "name_batch"


def test_step6_alt_harvest_falls_back_to_card_structured():
    profile = _fake_profile(needs_render=True, paginate=True, text_extraction="alt_harvest")
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 6
    assert strat.text_extraction == "card_structured"


def test_step7_main_prose_falls_back_to_full_text():
    profile = _fake_profile(needs_render=True, paginate=True, text_extraction="main_prose")
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 7
    assert strat.text_extraction == "full_text"


def test_ladder_position_skips_already_tried_steps():
    """A profile already at ladder position 2 must never retry step 1 or 2 again."""
    profile = _fake_profile(needs_render=True, paginate=False, retry_ladder_position=2)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=5)  # step 1 condition IS true...
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 3  # ...but position already passed 1 and 2, so step 3 is next
    assert strat.paginate is True


def test_ladder_exhausted_returns_none():
    profile = _fake_profile(
        needs_render=True, paginate=True, text_extraction="full_text",
        retry_ladder_position=7,  # every step already tried
    )
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=0)
    assert next_retry_step(profile, outcome, PipelineMetrics()) is None


def test_qwen_failures_checked_before_render_even_when_not_rendered():
    """The plan's core ordering guarantee: a timeout shortfall and a fetch
    shortfall look identical in the raw numbers but need opposite fixes."""
    profile = _fake_profile(needs_render=False, names_per_chunk=10)
    outcome = PageOutcome(url="https://x.test/a", qwen_failures=1)
    idx, strat = next_retry_step(profile, outcome, PipelineMetrics())
    assert idx == 1
    assert strat.names_per_chunk == 5
    assert strat.needs_render is False  # step 2 not reached this call


# ── apply_retry_result — DB round-trip ──────────────────────────────────────

@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def _cleanup(db, domain):
    db.query(SiteProfile).filter(SiteProfile.domain == domain).delete()
    db.commit()


def test_apply_retry_result_persists_on_success(db):
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        row = SiteProfile(domain=domain, url_pattern="")
        strat = PageStrategy(page_shape="logo_grid", text_extraction="card_structured",
                              chunking="per_card", confidence="high", source="deterministic")
        _apply_strategy(row, strat, source_id=None, signals={"text_len": 1})
        db.add(row)
        db.commit()
        db.refresh(row)

        new_strat = strat.with_(text_extraction="alt_harvest", chunking="name_batch")
        apply_retry_result(str(row.id), 5, new_strat, recovered=True)

        db.refresh(row)
        assert row.text_extraction == "alt_harvest"
        assert row.chunking_mode == "name_batch"
        assert row.strategy_source == "learned"
        assert row.retry_ladder_position == 5
        assert row.page_shape == "logo_grid"  # untouched by a retry
    finally:
        _cleanup(db, domain)


def test_apply_retry_result_only_advances_pointer_on_failure(db):
    domain = f"test-{uuid.uuid4().hex[:8]}.example"
    try:
        row = SiteProfile(domain=domain, url_pattern="")
        strat = PageStrategy(page_shape="logo_grid", text_extraction="card_structured",
                              chunking="per_card", confidence="high", source="deterministic")
        _apply_strategy(row, strat, source_id=None, signals={"text_len": 1})
        db.add(row)
        db.commit()
        db.refresh(row)

        new_strat = strat.with_(text_extraction="alt_harvest", chunking="name_batch")
        apply_retry_result(str(row.id), 5, new_strat, recovered=False)

        db.refresh(row)
        assert row.text_extraction == "card_structured"  # unchanged — the retry didn't help
        assert row.chunking_mode == "per_card"
        assert row.strategy_source == "deterministic"
        assert row.retry_ladder_position == 5  # pointer still advances so next time tries step 6
    finally:
        _cleanup(db, domain)
