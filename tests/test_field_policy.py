"""
Phase Z (12 Aug 2026) — shared field-resolution policy.

Pure-logic tests, no network/Ollama/DB. The four real cases below are the
ones that shaped better_freetext's design — sampled live from the pending
review queue before writing the rule, not invented afterward to fit it.
"""
from processing.field_policy import (
    better_freetext, is_empty, norm_value, split_proposal,
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
