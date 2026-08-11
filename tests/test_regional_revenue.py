"""
Phase RC-4b — revenue as a second, independent qualifying signal (11 Aug 2026).

Owner's own words: "the threshold is 25 million at least if its lower we will
not consider and skip ... the revenue needs to be the latest which is of the
latest year or previous year and needs to be authentic and true". Three
things are tested here, matching the three places that requirement landed:

  - regional.filters.meets_revenue_threshold  -- the tri-state gate itself
  - regional.triage.tier_for                  -- revenue as a FALLBACK signal
  - regional.enrich._parse_revenue             -- "authentic", never guessed

No network, no Ollama, no GPU: everything here is pure parsing/decision logic.
"""
from regional import enrich, triage
from regional.filters import meets_revenue_threshold


# ── meets_revenue_threshold: the tri-state gate ─────────────────────────────

def test_unknown_revenue_is_unknown_not_false():
    """Never silently read 'no data' as 'too small' -- the same discipline
    in_employee_band and the footprint proxy already apply."""
    assert meets_revenue_threshold(None, None) is None
    assert meets_revenue_threshold(None, 2026) is None


def test_fresh_revenue_above_floor_qualifies():
    assert meets_revenue_threshold(45.2, 2026, as_of_year=2026) is True
    assert meets_revenue_threshold(25.0, 2026, as_of_year=2026) is True   # floor itself counts


def test_fresh_revenue_below_floor_disqualifies():
    """Per the owner: 'if its lower we will not consider and skip' -- known
    and fresh but below EUR 25m is a definite False, not a shrug."""
    assert meets_revenue_threshold(24.9, 2026, as_of_year=2026) is False
    assert meets_revenue_threshold(3.0, 2026, as_of_year=2026) is False


def test_prior_year_still_counts_as_latest():
    """'the latest year or previous year' -- both count as fresh."""
    assert meets_revenue_threshold(30.0, 2025, as_of_year=2026) is True


def test_stale_revenue_is_unknown_not_disqualifying():
    """A real, well-sourced but two-year-old figure must not auto-disqualify
    a company -- it just can't be trusted for an automated decision."""
    assert meets_revenue_threshold(200.0, 2024, as_of_year=2026) is None


def test_undated_revenue_is_unknown():
    """No year attached at all -- can't tell if it's fresh, so never used."""
    assert meets_revenue_threshold(200.0, None) is None


def test_as_of_year_override_is_deterministic():
    """The test hook exists so freshness is testable without depending on
    wall-clock time. Same inputs, different as_of_year -> different verdict."""
    assert meets_revenue_threshold(50.0, 2020, as_of_year=2020) is True
    assert meets_revenue_threshold(50.0, 2020, as_of_year=2026) is None


# ── triage.tier_for: revenue as a FALLBACK, never an override ───────────────

def test_revenue_promotes_when_headcount_unknown():
    tier = triage.tier_for(employees=None, source="osm",
                           revenue_eur_millions=45.0, revenue_year=2026)
    assert tier == triage.TIER_READY


def test_revenue_below_floor_marks_out_of_band_when_headcount_unknown():
    tier = triage.tier_for(employees=None, source="osm",
                           revenue_eur_millions=5.0, revenue_year=2026)
    assert tier == triage.TIER_OUT_OF_BAND


def test_known_headcount_always_wins_over_revenue():
    """Employees decides first when known -- a huge revenue figure must not
    override a headcount that is genuinely outside the band, and a small
    revenue figure must not override one that's genuinely inside it."""
    out_of_band = triage.tier_for(employees=36_000, source="sheet",
                                  revenue_eur_millions=500.0, revenue_year=2026)
    assert out_of_band == triage.TIER_OUT_OF_BAND

    in_band = triage.tier_for(employees=300, source="osm",
                              revenue_eur_millions=1.0, revenue_year=2026)
    assert in_band == triage.TIER_READY


def test_stale_revenue_falls_through_to_footprint_and_source_logic():
    """An old/undated revenue figure resolves to None from
    meets_revenue_threshold, which must fall through to the existing
    footprint/source prior exactly as if revenue were never supplied."""
    with_source = triage.tier_for(employees=None, source="wikidata",
                                  revenue_eur_millions=200.0, revenue_year=2020)
    assert with_source == triage.TIER_ENRICH        # notability source still applies

    without_source = triage.tier_for(employees=None, source="osm",
                                     revenue_eur_millions=200.0, revenue_year=2020)
    assert without_source == triage.TIER_LOW_PRIOR  # falls all the way through


def test_large_footprint_still_promotes_when_revenue_unknown():
    """Unaffected sibling path -- confirms the revenue branch didn't
    accidentally swallow the footprint branch below it."""
    tier = triage.tier_for(employees=None, source="osm", footprint_m2=50_000.0)
    assert tier == triage.TIER_ENRICH


# ── enrich._parse_revenue: "authentic", never guessed ───────────────────────

def test_german_decimal_with_mio_unit_and_year():
    assert enrich._parse_revenue("45,2 Mio. EUR (2025)") == (45.2, 2025)


def test_english_style_with_million_unit():
    assert enrich._parse_revenue("128 Mio EUR 2024") == (128.0, 2024)


def test_billion_unit_converts_to_millions():
    assert enrich._parse_revenue("1,2 Mrd. Euro (2024)") == (1200.0, 2024)


def test_german_thousands_and_decimal_separators():
    assert enrich._parse_revenue("1.234,5 Mio. EUR (2025)") == (1234.5, 2025)


def test_raw_six_plus_digit_eur_amount_is_accepted():
    """No Mio/Mrd unit, but a long enough raw digit run is unambiguous."""
    assert enrich._parse_revenue("45000000 EUR (2025)") == (45.0, 2025)


def test_bare_unitless_small_number_is_rejected():
    """The core authenticity rule: never assume a bare '45' already means
    'millions' -- that guess would be off by a factor of a thousand if wrong."""
    assert enrich._parse_revenue("45") is None
    assert enrich._parse_revenue("45 (2025)") is None


def test_missing_year_is_still_parsed_but_year_is_none():
    """An undated figure is still stored (a human can see it) -- the caller,
    not the parser, decides it can't be used to auto-qualify."""
    assert enrich._parse_revenue("45,2 Mio. EUR") == (45.2, None)


def test_implausible_magnitude_is_rejected():
    assert enrich._parse_revenue("0.001 Mio. EUR (2025)") is None       # too small
    assert enrich._parse_revenue("5000000 Mio. EUR (2025)") is None     # larger than VW Group globally


def test_garbage_input_returns_none():
    assert enrich._parse_revenue(None) is None
    assert enrich._parse_revenue("") is None
    assert enrich._parse_revenue("keine Angabe") is None


# ── proposals_from_verdict / apply_proposals: revenue end-to-end ────────────

def _co(**kw):
    import types
    base = dict(name="Test GmbH", city="Memmingen", employees=None,
               branche=None, kurzbeschreibung=None, website=None,
               revenue_eur_millions=None, revenue_year=None,
               field_sources=None, last_verified_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_revenue_field_is_understood_via_german_aliases():
    v = {"identity_match": True, "findings": [
        {"field": "Umsatz", "verdict": "contradicted",
         "correct_value": "45,2 Mio. EUR (2025)", "source_url": "https://x.de"}]}
    out = enrich.proposals_from_verdict(_co(), v)
    assert out["revenue_eur_millions"]["value"] == 45.2
    assert out["revenue_eur_millions"]["year"] == 2025
    assert out["revenue_eur_millions"]["source_url"] == "https://x.de"


def test_unparseable_revenue_yields_no_proposal():
    v = {"identity_match": True, "findings": [
        {"field": "revenue", "verdict": "contradicted",
         "correct_value": "keine Angabe", "source_url": "https://x.de"}]}
    assert "revenue_eur_millions" not in enrich.proposals_from_verdict(_co(), v)


def test_apply_proposals_sets_both_revenue_fields_from_one_proposal():
    co = _co()
    res = enrich.apply_proposals(co, {
        "revenue_eur_millions": {"value": 45.2, "year": 2025,
                                 "source_url": "https://unglehrt.de/u.html"}})
    assert co.revenue_eur_millions == 45.2
    assert co.revenue_year == 2025
    assert co.field_sources["revenue_eur_millions"] == "https://unglehrt.de/u.html"
    assert res["filled"] == ["revenue_eur_millions"]


def test_apply_proposals_never_overwrites_existing_revenue():
    co = _co(revenue_eur_millions=100.0, revenue_year=2026)
    res = enrich.apply_proposals(co, {
        "revenue_eur_millions": {"value": 999.0, "year": 2025,
                                 "source_url": "https://x.de"}})
    assert co.revenue_eur_millions == 100.0 and co.revenue_year == 2026
    assert res["proposed_only"] == ["revenue_eur_millions"]
