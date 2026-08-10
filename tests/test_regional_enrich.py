"""
Phase RC-4 — regional enrichment guards.

No network, no Ollama, no GPU: every case exercises the pure parsing and
write-policy functions against verdict shapes actually returned by the model
during the 7 Aug live run.
"""
import types

from regional import enrich


def _co(**kw):
    """A stand-in for a RegionalCompany row. enrich.py is duck-typed by
    design, so a namespace is enough and no DB is involved."""
    base = dict(name="Test GmbH", city="Memmingen", employees=None,
                branche=None, kurzbeschreibung=None, website=None,
                field_sources=None, last_verified_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── the query must not be startup-shaped ────────────────────────────────────

def test_query_is_sme_shaped_not_startup_shaped():
    """processing/web_verifier.py appends the literal words 'startup founded'
    to every query. For a 120-year-old Maschinenbau firm that actively skews
    the results, which is the whole reason this module exists separately."""
    q = enrich.build_query(_co(name="Unglehrt", city="Memmingen"))
    assert "Unglehrt" in q and "Memmingen" in q
    assert "startup" not in q.lower()


# ── website normalisation (a real bug found live) ───────────────────────────

def test_bare_domain_is_accepted_not_rejected():
    """Live 7 Aug: the model returned 'stoba.one' and the scheme check
    rejected a perfectly good company domain as 'non-official'."""
    v = {"identity_match": True, "findings": [
        {"field": "website", "verdict": "contradicted",
         "correct_value": "stoba.one", "source_url": "https://stoba.one/x"}]}
    out = enrich.proposals_from_verdict(_co(), v)
    assert out["website"]["value"] == "https://stoba.one"


def test_aggregator_and_social_websites_are_rejected():
    for bad in ("https://www.linkedin.com/company/x", "https://de.wikipedia.org/wiki/X",
                "https://www.northdata.de/x", "https://www.kununu.com/x",
                "https://www.wer-zu-wem.de/firma/x.html"):
        v = {"identity_match": True, "findings": [
            {"field": "website", "verdict": "contradicted",
             "correct_value": bad, "source_url": bad}]}
        assert "website" not in enrich.proposals_from_verdict(_co(), v)


def test_non_domain_garbage_is_rejected():
    v = {"identity_match": True, "findings": [
        {"field": "website", "verdict": "contradicted",
         "correct_value": "not a url", "source_url": ""}]}
    assert "website" not in enrich.proposals_from_verdict(_co(), v)


# ── employee parsing ────────────────────────────────────────────────────────

def test_employee_values_are_parsed_and_sanity_checked():
    def emp(val):
        v = {"identity_match": True, "findings": [
            {"field": "employees", "verdict": "contradicted",
             "correct_value": val, "source_url": "https://x.de"}]}
        return enrich.proposals_from_verdict(_co(), v).get("employees", {}).get("value")

    assert emp("200") == 200
    assert emp("ca. 450 Mitarbeiter") == 450
    assert emp("1.200") == 1200
    # The model occasionally puts a revenue figure in the headcount slot.
    assert emp("850000000") is None
    assert emp("keine Angabe") is None


def test_german_field_aliases_are_understood():
    v = {"identity_match": True, "findings": [
        {"field": "Mitarbeiter", "verdict": "contradicted",
         "correct_value": "300", "source_url": "https://x.de"},
        {"field": "Branche", "verdict": "contradicted",
         "correct_value": "Maschinenbau", "source_url": "https://x.de"}]}
    out = enrich.proposals_from_verdict(_co(), v)
    assert out["employees"]["value"] == 300
    assert out["branche"]["value"] == "Maschinenbau"


# ── identity guard ──────────────────────────────────────────────────────────

def test_wrong_company_yields_nothing():
    """A same-name company elsewhere must not contaminate the record."""
    v = {"identity_match": False, "summary": "different company",
         "findings": [{"field": "employees", "verdict": "contradicted",
                       "correct_value": "9999", "source_url": "https://x.de"}]}
    assert enrich.proposals_from_verdict(_co(), v) == {}


def test_unknown_fields_are_ignored():
    v = {"identity_match": True, "findings": [
        {"field": "funding_stage", "verdict": "contradicted",
         "correct_value": "Series A", "source_url": "https://x.de"}]}
    assert enrich.proposals_from_verdict(_co(), v) == {}


# ── write policy: fill empties, never silently overwrite ────────────────────

def test_empty_fields_are_filled_with_a_citation():
    co = _co()
    res = enrich.apply_proposals(co, {
        "employees": {"value": 200, "source_url": "https://unglehrt.de/u.html"},
        "branche": {"value": "Bau", "source_url": "https://de.wikipedia.org/x"}})
    assert co.employees == 200 and co.branche == "Bau"
    assert co.field_sources["employees"] == "https://unglehrt.de/u.html"
    assert set(res["filled"]) == {"employees", "branche"}
    assert co.last_verified_at is not None


def test_existing_values_are_never_silently_overwritten():
    """The project's standing rule: evidence is gathered, humans decide."""
    co = _co(employees=450, branche="Maschinenbau")
    res = enrich.apply_proposals(co, {
        "employees": {"value": 999, "source_url": "https://x.de"}})
    assert co.employees == 450                      # unchanged
    assert res["proposed_only"] == ["employees"]
    stashed = co.field_sources["proposed_employees"]
    assert stashed["value"] == 999 and stashed["current"] == "450"


def test_agreement_with_an_existing_value_is_a_no_op():
    co = _co(branche="Maschinenbau")
    res = enrich.apply_proposals(co, {
        "branche": {"value": "Maschinenbau", "source_url": "https://x.de"}})
    assert res["filled"] == [] and res["proposed_only"] == []
    assert co.branche == "Maschinenbau"


def test_prior_citations_are_preserved():
    co = _co(field_sources={"website": "https://old.de"})
    enrich.apply_proposals(co, {
        "branche": {"value": "Bau", "source_url": "https://new.de"}})
    assert co.field_sources["website"] == "https://old.de"
    assert co.field_sources["branche"] == "https://new.de"


# ── --all-tiers selection mode ──────────────────────────────────────────────

def test_all_tiers_mode_ignores_triage_tier_and_targets_missing_description():
    """'At least get a description on everyone' means the tier gate — which
    exists to withhold the EXPENSIVE headcount hunt from tier 3's ~875 mostly
    small OSM finds — must not also withhold the cheap Kurzbeschreibung field
    the same call already asks for."""
    import argparse
    import importlib.util
    import pathlib

    from database.connection import SessionLocal
    from database.models import RegionalCompany
    from regional import triage

    spec = importlib.util.spec_from_file_location(
        "rc_enrich_mod", pathlib.Path(__file__).parent.parent / "scripts" / "rc_enrich.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    prefix = "PYTEST-RCENRICH-ALLTIERS"
    db = SessionLocal()
    try:
        db.query(RegionalCompany).filter(
            RegionalCompany.name.like(f"{prefix}%")).delete(synchronize_session=False)
        db.add_all([
            RegionalCompany(name=f"{prefix} Tier3 NoDesc", normalized_name="x1",
                            in_radius=True, triage_tier=triage.TIER_LOW_PRIOR,
                            kurzbeschreibung=None, source="osm", distance_km=5.0),
            RegionalCompany(name=f"{prefix} Tier1 HasDesc", normalized_name="x2",
                            in_radius=True, triage_tier=triage.TIER_READY,
                            kurzbeschreibung="already has one", employees=200,
                            source="sheet", distance_km=1.0),
            RegionalCompany(name=f"{prefix} Tier9 NoDesc", normalized_name="x3",
                            in_radius=True, triage_tier=triage.TIER_OUT_OF_BAND,
                            kurzbeschreibung=None, employees=50000,
                            source="wikidata", distance_km=2.0),
        ])
        db.commit()

        args = argparse.Namespace(name=None, all_tiers=True, limit=100, tier=triage.TIER_ENRICH)
        selected = {c.name for c in mod._select(db, args)}

        # Missing a description, regardless of tier (including tier 9/3):
        assert f"{prefix} Tier3 NoDesc" in selected
        assert f"{prefix} Tier9 NoDesc" in selected
        # Already has one -> not selected, even though it's tier 1:
        assert f"{prefix} Tier1 HasDesc" not in selected
    finally:
        db.query(RegionalCompany).filter(
            RegionalCompany.name.like(f"{prefix}%")).delete(synchronize_session=False)
        db.commit()
        db.close()
