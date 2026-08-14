"""
Phase Z (12 Aug 2026) — shared field-resolution policy.

Pure-logic tests, no network/Ollama/DB. The four real cases below are the
ones that shaped better_freetext's design — sampled live from the pending
review queue before writing the rule, not invented afterward to fit it.
"""
from processing.field_policy import (
    _detect_language, better_freetext, is_empty, norm_value, safe_string_list, split_proposal,
)


# ── better_freetext: the four real cases that shaped this rule ─────────────

def test_heliatek_upgrade_takes_the_richer_new_value():
    """A near-empty placeholder replaced by a real description -- new wins."""
    old = "Organische Solarfolien."
    new = ("Entwickelt und produziert leichte, flexible und transparente "
          "organische Photovoltaik-Filme, die in verschiedene Baustoffe "
          "integriert werden können.")
    assert better_freetext(old, new) == new


def test_knowmanity_downgrade_keeps_the_richer_old_value():
    """A detailed description must NOT be replaced by a vaguer one just
    because a later re-crawl produced it -- length/richness decides, not
    recency."""
    old = ("Knowmanity solves the problem of knowledge loss by using an "
          "AI-driven interview process to convert expert knowledge into a "
          "living digital twin that can be queried via chat with source "
          "references.")
    new = "Preserves valuable corporate knowledge."
    assert better_freetext(old, new) == old


def test_trustyou_near_identical_reword_is_a_no_op():
    """92%-similar rewording (one founder's name dropped) is noise, not a
    real change -- must return None so nothing gets staged OR overwritten."""
    old = ("TrustYou, founded in Munich, Germany, in 2008 by Benjamin Jost "
          "and Jakob Riegger, operates as a comprehensive AI-powered "
          "hospitality platform.")
    new = ("TrustYou, founded in Munich, Germany, in 2008 by Benjamin Jost, "
          "operates as a comprehensive AI-powered hospitality platform.")
    assert better_freetext(old, new) is None


def test_genuine_tie_keeps_old_not_churn():
    """Two different but comparably-detailed descriptions -- below the 20%
    length margin, so we keep what we have rather than flip-flop forever."""
    old = "Builds AI agents for insurance companies handling claims intake."
    new = "Offers AI agents for insurance companies handling claims intake."
    assert better_freetext(old, new) == old


def test_carboninsights_language_swap_is_rejected():
    """The real case found live dry-running the backlog drain, BEFORE this
    guard existed: a longer German candidate would have silently replaced a
    real English description purely on length. 95 of 339 non-empty applies
    in the live backlog were this shape (28%) -- length isn't a valid
    informativeness proxy across a language boundary."""
    old = "Automates CO2 emission data collection and analysis for businesses."
    new = ("CarbonInsights automatisiert die Erfassung und Strukturierung von "
          "Emissionsdaten aus Geschäftsunterlagen, um CO2-Bilanzierungen für "
          "Unternehmen zu vereinfachen.")
    assert better_freetext(old, new) == old


def test_language_swap_rejected_in_either_direction():
    old_de = "Automatisiert die Buchhaltung für kleine Unternehmen in Deutschland."
    new_en = "Automates bookkeeping for small businesses across Europe with AI."
    assert better_freetext(old_de, new_en) == old_de


# ── better_freetext: boundary behaviour ─────────────────────────────────────

def test_empty_old_is_a_plain_fill():
    assert better_freetext(None, "A real description.") == "A real description."
    assert better_freetext("", "A real description.") == "A real description."


def test_empty_new_never_blanks_a_real_value():
    assert better_freetext("A real description.", None) is None
    assert better_freetext("A real description.", "") is None


def test_both_empty_is_a_no_op():
    assert better_freetext(None, None) is None
    assert better_freetext("", "") is None


def test_length_margin_boundary():
    old = "x" * 100
    just_under = "y" * 119   # < 1.20x -> keep old
    just_over = "y" * 121    # >= 1.20x -> take new
    assert better_freetext(old, just_under) == old
    assert better_freetext(old, just_over) == just_over


# ── is_empty / split_proposal ───────────────────────────────────────────────

def test_is_empty_matches_web_verifier_semantics():
    assert is_empty(None) and is_empty("") and is_empty(0)
    assert not is_empty("Munich") and not is_empty(2020)


def test_split_proposal_separates_fills_from_conflicts():
    proposed = {
        "city": {"old": None, "new": "Munich"},
        "funding_stage": {"old": "Seed", "new": "Series A"},
    }
    fills, conflicts = split_proposal(proposed)
    assert set(fills) == {"city"}
    assert set(conflicts) == {"funding_stage"}


# ── norm_value: locale variants that were re-staging forever ───────────────

def test_norm_value_collapses_german_english_country_pair():
    assert norm_value("Germany") == norm_value("Deutschland")


def test_norm_value_collapses_city_diacritics():
    assert norm_value("Munich") == norm_value("München")
    assert norm_value("Zurich") == norm_value("Zürich")


def test_norm_value_does_not_collapse_genuinely_different_values():
    """The point of an explicit synonym map, not fuzzy matching: short,
    distinct funding-stage labels must never be conflated."""
    assert norm_value("Seed") != norm_value("Series A")
    assert norm_value("Munich") != norm_value("Berlin")


def test_norm_value_handles_none_and_whitespace():
    assert norm_value(None) == ""
    assert norm_value("  Munich  ") == norm_value("Munich")


# ── _detect_language: never guesses when inconclusive ───────────────────────

def test_detect_language_recognizes_german():
    assert _detect_language("Wir bauen eine Plattform für Unternehmen und Kunden.") == "de"


def test_detect_language_recognizes_english():
    assert _detect_language("We are a company that provides software for the team.") == "en"


def test_detect_language_inconclusive_on_a_bare_name():
    """A short value with no real stopwords must return None, not guess —
    e.g. a bare product name shouldn't block a legitimate same-language
    upgrade just because it happens to share a token with both lists."""
    assert _detect_language("Sovaro GmbH") is None


# ── safe_string_list: never shred a tags/founders string into characters ────
# Regression for a real bug found live 14 Aug 2026: ~100 Startup.tags rows
# ended up as ['[', "'", 'D', 'e', 'e', 'p', ...] instead of ['Deeptech',
# 'KI'] because several call sites did a bare list(x or [])/set(x or [])
# on a value that was, at some point, a plain string -- which Python
# iterates character-by-character. See safe_string_list's own docstring and
# scripts/repair_shredded_list_fields.py for the full incident.

def test_safe_string_list_passes_through_a_real_list():
    assert safe_string_list(["Deeptech", "KI"]) == ["Deeptech", "KI"]


def test_safe_string_list_drops_blank_and_non_string_entries():
    assert safe_string_list(["Deeptech", "", "  ", "KI"]) == ["Deeptech", "KI"]


def test_safe_string_list_none_and_empty():
    assert safe_string_list(None) == []
    assert safe_string_list([]) == []
    assert safe_string_list("") == []
    assert safe_string_list("   ") == []


def test_safe_string_list_recovers_a_shredded_list():
    """The exact real shape found in the DB: every element is a single
    character, forming "['Deeptech', 'KI']" when rejoined."""
    shredded = ["[", "'", "D", "e", "e", "p", "t", "e", "c", "h", "'", ",",
               " ", "'", "K", "I", "'", "]"]
    assert safe_string_list(shredded) == ["Deeptech", "KI"]


def test_safe_string_list_recovers_a_bare_stringified_list():
    assert safe_string_list("['Deeptech', 'KI']") == ["Deeptech", "KI"]


def test_safe_string_list_treats_a_plain_string_as_one_tag_not_characters():
    """The critical never-regress case: a bare string that is NOT a
    list-repr must become one single-item list, never get iterated into
    individual characters."""
    assert safe_string_list("München") == ["München"]
    assert safe_string_list("brandneu") == ["brandneu"]
