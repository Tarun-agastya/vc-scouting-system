"""
Phase R-1 — structural inspector.

These lock in the two failure modes that motivated the whole redesign, using
synthetic HTML so they run offline and fast:
  - a name-only logo grid (zollhof shape) must be found even though its items
    carry no links at all
  - a news/events feed (schwaben, startbase, sce shape) must NOT become an
    entity list with a recall expectation
"""
from bs4 import BeautifulSoup

from config.tuning_loader import get_inspector_config
from ingestion.site_inspector import (
    probe_html, detect_card_groups, derive_strategy_deterministic,
    _is_headline_like,
)

CFG = get_inspector_config()


def _logo_grid_html(n=40):
    cards = "".join(
        f'<div class="portfolio-box"><div class="background-img">'
        f'<img src="/l/{i}.svg" alt="Company{i} Tech"></div></div>'
        for i in range(n)
    )
    return f"<html><body><main><div class='portfolio'>{cards}</div></main></body></html>"


def _news_feed_html(n=12):
    cards = "".join(
        f'<article class="post"><a href="/news-details/story-{i}">'
        f'<h3>Startup XY raises {i} million euros in a Series A round this week</h3>'
        f'<p>{"body text " * 30}</p></a></article>'
        for i in range(n)
    )
    return f"<html><body><main><div class='feed'>{cards}</div></main></body></html>"


def _card_directory_html(n=15):
    cards = "".join(
        f'<div class="company-card"><a href="/companies/firm-{i}">'
        f'<h3>Firm {i} GmbH</h3><p>Builds industrial software for factories.</p></a></div>'
        for i in range(n)
    )
    return f"<html><body><main><div class='grid'>{cards}</div></main></body></html>"


# ── Logo grid (the zollhof shape) ────────────────────────────────────────────

def test_logo_grid_detected_without_any_links():
    """The flagged risk: logo items carry no <a>, so uniqueness-by-href is 0.
    max(unique_href, unique_name) must carry it."""
    sig = probe_html(_logo_grid_html(40), "https://x.test/portfolio", CFG)
    assert sig.primary_group is not None
    g = sig.primary_group
    assert g.n == 40
    assert g.frac_with_link == 0.0          # genuinely no links
    assert g.frac_unique_href == 0.0
    assert g.frac_unique_name > 0.9         # ...but names are distinct
    assert g.name_only is True


def test_logo_grid_strategy_is_alt_harvest_with_expectation():
    sig = probe_html(_logo_grid_html(40), "https://x.test/portfolio", CFG)
    st = derive_strategy_deterministic(sig, CFG)
    assert st.page_shape == "logo_grid"
    assert st.text_extraction == "alt_harvest"
    assert st.chunking == "name_batch"
    assert st.bypass_candidate_filter is True
    assert st.expected_entity_count == 40
    assert st.expects_entities is True


# ── News/events feed (the schwaben / startbase / sce shape) ──────────────────

def test_news_feed_is_not_an_entity_directory():
    """The regression that wrote 72 junk records: an editorial feed must never
    become a company list, and must never claim a recall expectation."""
    sig = probe_html(_news_feed_html(12), "https://x.test/", CFG)
    st = derive_strategy_deterministic(sig, CFG)
    assert st.page_shape == "article_feed"
    assert st.text_extraction != "alt_harvest"
    assert st.expected_entity_count == 0
    assert st.expects_entities is False


def test_editorial_path_segments_are_tokenised():
    """sce.de emits /news-details/*; an exact-segment match misses it."""
    sig = probe_html(_news_feed_html(8), "https://x.test/", CFG)
    assert sig.detail_link_pattern is not None
    assert "news" in sig.detail_link_pattern
    assert derive_strategy_deterministic(sig, CFG).page_shape == "article_feed"


# ── Real card directory ──────────────────────────────────────────────────────

def test_card_directory_keeps_its_expectation():
    sig = probe_html(_card_directory_html(15), "https://x.test/companies", CFG)
    st = derive_strategy_deterministic(sig, CFG)
    assert st.page_shape == "card_directory"
    assert st.chunking == "per_card"
    assert st.expected_entity_count == 15
    assert st.expects_entities is True


# ── Name-shape discriminator ─────────────────────────────────────────────────

def test_company_names_are_not_headline_like():
    for name in ("Nouma Autonomy", "Lemvos", "WeSort.AI GmbH", "bill.less",
                 "e-laborate", "SportBrain Entertainment GmbH", "F-50"):
        assert not _is_headline_like(name), name


def test_headlines_are_detected():
    for name in ("BISON listet acht neue Kryptowährungen und baut Angebot weiter aus",
                 "1 Million Euro ARR in 15 Monaten: Silvernova wächst rasant",
                 "You’re a Startup looking for an accelerator?"):
        assert _is_headline_like(name), name


def test_standalone_period_terminated_sentences_are_detected():
    """Regression for a bug found live 14 Aug 2026: munich-startup.de's own
    startup-signup form has a password-requirements checklist (one short
    German sentence per card: "Enthält einen Großbuchstaben.") that scored as
    a logo grid and got harvested as six fake "startup names," each spawning
    a possible_duplicate review against an unrelated real company. The old
    _SENTENCE_PUNCT regex only matched a period FOLLOWED BY more text
    (mid-sentence), so a short sentence that IS the whole card text -- ending
    the string with nothing after the period -- slipped through undetected."""
    for name in ("Enthält einen Großbuchstaben.", "Enthält einen Kleinbuchstaben.",
                 "Enthält eine Zahl.", "Enthält ein Sonderzeichen.",
                 "Mindestens 8 Zeichen lang.", "Weniger als 70 Zeichen lang."):
        assert _is_headline_like(name), name

    # Single-token names ending in a period (abbreviation or domain, not a
    # sentence) must NOT be caught by the new branch -- no space, no match.
    for name in ("K.I.T.", "titanspear.ai"):
        assert not _is_headline_like(name), name


# ── Degenerate input must never raise ────────────────────────────────────────

def test_empty_and_junk_html_are_safe():
    for html in ("", "<html></html>", "not html at all"):
        sig = probe_html(html, "https://x.test/", CFG)
        assert sig.candidate_entity_count == 0
        st = derive_strategy_deterministic(sig, CFG)
        assert st.expected_entity_count == 0
        # falls back to today's behaviour, never worse
        assert st.text_extraction in ("full_text", "main_prose")


def _breadcrumb_html():
    # uni-augsburg.de's real shape (3 Aug, R-2 batch verification): a plain
    # <ol class="breadcrumbs">, no <nav> tag, no ARIA role/label at all.
    items = "".join(
        f'<li class="breadcrumbs-item"><a href="/p{i}">{n}</a></li>'
        for i, n in enumerate(["Universität", "Organisation", "Einrichtungen", "Startseite", "Startseite"])
    )
    return f'<html><body><main><ol class="breadcrumbs pb-3">{items}</ol></main></body></html>'


def _partner_logo_grid_html(n=6):
    # cdtm.de's real shape: alt text literally says "<Name> logo", not just
    # the name — a common accessibility convention for a partner/sponsor strip.
    unis = ["MIT", "Cambridge", "Harvard", "Stanford", "Berkeley", "Tsinghua"]
    cards = "".join(
        f'<div class="partner-item"><img src="/l/{i}.svg" alt="{unis[i]} logo"></div>'
        for i in range(n)
    )
    return f"<html><body><main><div class='partners'>{cards}</div></main></body></html>"


def test_breadcrumb_trail_is_not_an_entity_group():
    """Regression: <ol class="breadcrumbs"> (no <nav>, no ARIA) was card-detected
    as a 5-entity logo_grid because \\bbreadcrumb\\b can't match inside the plural
    "breadcrumbs" — found live on uni-augsburg.de, 3 Aug."""
    sig = probe_html(_breadcrumb_html(), "https://x.test/a/b/c", CFG)
    st = derive_strategy_deterministic(sig, CFG)
    assert st.expected_entity_count == 0
    assert st.page_shape != "logo_grid"


def test_partner_university_logos_are_filtered():
    """Regression: alt text like "MIT logo" passed through whole — _ALT_NOISE
    only exact-matched the bare word "logo". Found live on cdtm.de, 3 Aug."""
    sig = probe_html(_partner_logo_grid_html(6), "https://x.test/partners", CFG)
    # Either no group clears threshold, or if one does, none of its names still
    # carry the literal "logo" caption.
    if sig.primary_group is not None:
        assert all("logo" not in n.lower() for n in sig.primary_group.names())


def test_nav_only_page_finds_no_entity_group():
    html = ("<html><body><nav><ul>"
            + "".join(f'<li class="menu-item"><a href="/p{i}">Page {i}</a></li>'
                      for i in range(8))
            + "</ul></nav></body></html>")
    sig = probe_html(html, "https://x.test/", CFG)
    groups = detect_card_groups(BeautifulSoup(html, "html.parser"), CFG)
    # a nav may still be a "group", but it must not win as the primary entity list
    if sig.primary_group is not None:
        assert sig.primary_group.in_nav_or_footer or sig.candidate_entity_count == 0
    assert derive_strategy_deterministic(sig, CFG).expected_entity_count == 0 or groups
