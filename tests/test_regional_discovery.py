"""
Phase RC-2 — discovery filtering and matching.

Every case here is one that actually occurred in a live sweep (6-7 Aug), not
an invented one. Network is never touched: the source adapters are exercised
against captured payload shapes.
"""
import pytest

from regional import discovery, filters, matcher


# ── the inversion that matters most ─────────────────────────────────────────

def test_large_incumbents_are_targets_here_not_junk():
    """Phase J drops Liebherr/PERI/Goldbeck because they are not startups.
    This register wants exactly those — Liebherr-Verzahntechnik (Kempten, 900)
    and Liebherr-Hydraulikbagger (Kirchdorf, 10 km) were two of the best real
    finds. Regressing this would gut the register."""
    for name in ("Liebherr-Verzahntechnik", "Liebherr-Hydraulikbagger",
                 "PERI", "Goldbeck", "Ed. Züblin"):
        assert filters.accept(name), f"{name} must NOT be filtered here"


def test_institutions_are_rejected():
    for name in ("Sparkasse Memmingen", "VR-Bank Memmingen eG",
                 "Raiffeisenbank Krumbach/Schwaben", "Volksbank Ulm-Biberach",
                 "IHK Schwaben", "Handwerkskammer Augsburg",
                 "Sonntag und Partner Rechtsanwälte", "BKK Verbundplus",
                 "Stadtwerke Ulm/Neu-Ulm", "SWU Verkehr",
                 "Technische Werke Schussental", "Erdgas Südwest",
                 "Hochschule Kempten", "Klinikum Memmingen"):
        assert not filters.accept(name), f"{name} should have been filtered"


def test_word_boundary_prevents_false_rejects():
    """A loose substring match would kill real companies: 'Bankmann' contains
    'bank', 'Kammerer' contains 'kammer'."""
    for name in ("Bankmann GmbH", "Kammerer Präzision", "Verkehrt Design GmbH"):
        assert filters.accept(name), f"{name} wrongly rejected"


def test_structural_junk_rejected():
    assert not filters.accept("menstruflow, Nouxx, nghty berlin, Olena Scent")
    assert not filters.accept("Industrie 4.0: Wie Startups die Industrie ändern")
    assert not filters.accept("eine Digitalagentur")
    assert not filters.accept("Q2706363")          # unlabelled Wikidata item
    assert not filters.accept("")


def test_wikipedia_disambiguation_is_stripped_not_rejected():
    assert filters.clean_name("Wanzl (Unternehmen)") == "Wanzl"
    assert filters.accept("Wanzl (Unternehmen)")


# ── employee band ───────────────────────────────────────────────────────────

def test_band_excludes_both_ends():
    """Measured live: Schlecker (36,000, and defunct) and Müller (33,635) both
    arrived 'in radius with a headcount'. A floor alone would have kept them."""
    assert filters.in_employee_band(650)          # Goldhofer
    assert filters.in_employee_band(100)          # inclusive floor
    assert filters.in_employee_band(4000)         # inclusive ceiling
    assert not filters.in_employee_band(33635)    # Müller
    assert not filters.in_employee_band(50)
    assert not filters.in_employee_band(None)     # unknown is not "in band"


# ── matching ────────────────────────────────────────────────────────────────

def test_legal_form_variants_are_the_same_company():
    assert matcher.is_same("Josef Hebel", "Josef Hebel GmbH & Co. KG")
    assert matcher.is_same("Wanzl", "Wanzl GmbH")
    assert matcher.is_same("myonic", "myonic GmbH")
    assert matcher.is_same("Sensortechnik Wiedemann",
                           "Sensor-Technik Wiedemann GmbH")


def test_different_companies_sharing_a_surname_stay_separate():
    """Both are real rows on the sheet; merging them would lose a prospect."""
    assert not matcher.is_same("Schmid GmbH", "Hubert Schmid Bauunternehmen")


def test_short_names_do_not_match_on_containment():
    """'SFB' must not swallow anything containing those letters."""
    assert not matcher.is_same("SFB", "SFB Industrieanlagen Rosenheim GmbH & Co")


def _cand(name, **kw):
    return discovery.Candidate(name=name, source=kw.pop("source", "test"), **kw)


def test_dedupe_keeps_the_richest_record_and_merges_gaps():
    cands = [
        _cand("Wanzl", source="wikipedia"),
        _cand("Wanzl GmbH", source="wikidata", employees=650,
              lat=48.13, lon=10.47, city="Kirchheim"),
        _cand("Wanzl", source="osm", website="https://wanzl.com"),
    ]
    kept, collapsed = matcher.dedupe(cands)
    assert len(kept) == 1 and collapsed == 2
    win = kept[0]
    assert win.employees == 650          # richest record won
    assert win.website == "https://wanzl.com"   # gap filled from a poorer one
    assert set(win.raw["also_seen_in"]) >= {"wikipedia", "osm"}


def test_split_new_vs_known_uses_fuzzy_matching():
    cands = [_cand("Goldhofer"), _cand("Josef Hebel GmbH & Co. KG")]
    new, known = matcher.split_new_vs_known(cands, ["Josef Hebel GmbH"])
    assert [c.name for c in new] == ["Goldhofer"]
    assert len(known) == 1


# ── candidate geometry ──────────────────────────────────────────────────────

def test_distance_and_radius_bucketing():
    inside = _cand("Near", lat=47.9878, lon=10.1815)
    far = _cand("Far", lat=48.3705, lon=10.8978)      # Augsburg
    nogeo = _cand("Unknown")

    assert inside.distance_km == 0.0
    assert far.distance_km > 60

    buckets = discovery.filter_candidates([inside, far, nogeo], radius_km=50)
    assert [c.name for c in buckets["kept"]] == ["Near"]
    assert [c.name for c in buckets["out_of_radius"]] == ["Far"]
    # Wikipedia candidates have no coordinates — dropping them would discard
    # that whole source, so they are kept for later geocoding.
    assert [c.name for c in buckets["needs_geocoding"]] == ["Unknown"]


def test_institutions_are_dropped_before_geography():
    buckets = discovery.filter_candidates(
        [_cand("Sparkasse Memmingen", lat=47.9878, lon=10.1815)], radius_km=50)
    assert len(buckets["junk"]) == 1
    assert not buckets["kept"]


# ── source adapters, against captured payload shapes ────────────────────────

def test_wikidata_parses_coords_and_employees(monkeypatch):
    payload = {"results": {"bindings": [{
        "company": {"value": "http://www.wikidata.org/entity/Q878509"},
        "companyLabel": {"value": "Goldhofer"},
        "employees": {"value": "650"},
        "placeLabel": {"value": "Memmingen"},
        "coord": {"value": "Point(10.1815 47.9878)"},
    }]}}
    monkeypatch.setattr(discovery, "_get_json", lambda *a, **k: payload)
    out = discovery.from_wikidata()
    assert len(out) == 1
    c = out[0]
    assert c.name == "Goldhofer" and c.employees == 650
    assert c.lat == pytest.approx(47.9878) and c.lon == pytest.approx(10.1815)


def test_osm_empty_payload_is_a_failure_not_an_answer(monkeypatch):
    """A mirror returning HTTP 200 with zero elements previously looked
    identical to 'there are no factories near Memmingen'."""
    calls = []

    def fake(url, **kw):
        calls.append(url)
        if len(calls) < len(discovery.OVERPASS_MIRRORS):
            return {"elements": []}          # empty -> must try the next
        return {"elements": [{
            "type": "way", "id": 1,
            "center": {"lat": 47.99, "lon": 10.18},
            "tags": {"name": "HAWE Hydraulik", "man_made": "works"},
        }]}

    monkeypatch.setattr(discovery, "_get_json", fake)
    out = discovery.from_osm()
    assert len(calls) == len(discovery.OVERPASS_MIRRORS)   # kept trying
    assert [c.name for c in out] == ["HAWE Hydraulik"]


def test_osm_all_mirrors_failing_degrades_quietly(monkeypatch):
    def boom(*a, **k):
        raise OSError("504")
    monkeypatch.setattr(discovery, "_get_json", boom)
    assert discovery.from_osm() == []      # no exception escapes


def test_discover_survives_one_dead_source(monkeypatch):
    monkeypatch.setattr(discovery, "from_wikidata",
                        lambda **kw: [_cand("Goldhofer", source="wikidata")])
    monkeypatch.setattr(discovery, "from_wikipedia",
                        lambda **kw: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(discovery, "from_osm", lambda **kw: [])
    monkeypatch.setattr(discovery, "SOURCES", {
        "wikidata": discovery.from_wikidata,
        "wikipedia": discovery.from_wikipedia,
        "osm": discovery.from_osm,
    })
    out = discovery.discover()
    assert [c.name for c in out] == ["Goldhofer"]


# ── triage tiers ────────────────────────────────────────────────────────────

def test_triage_prioritises_by_source_when_size_is_unknown():
    """The measured problem: after the first sweep, 1,221 records had no
    headcount — spanning a 900-employee Liebherr plant and a one-person web
    design shop. Source is the only size signal available pre-enrichment."""
    from regional import triage

    # Known size decides outright.
    assert triage.tier_for(employees=650, source="osm") == triage.TIER_READY
    assert triage.tier_for(employees=33635, source="wikidata") == triage.TIER_OUT_OF_BAND
    assert triage.tier_for(employees=12, source="wikidata") == triage.TIER_OUT_OF_BAND

    # Unknown size falls back to how notability-filtered the source is.
    assert triage.tier_for(employees=None, source="wikidata") == triage.TIER_ENRICH
    assert triage.tier_for(employees=None, source="wikipedia") == triage.TIER_ENRICH
    assert triage.tier_for(employees=None, source="sheet") == triage.TIER_ENRICH
    assert triage.tier_for(employees=None, source="osm") == triage.TIER_LOW_PRIOR
    assert triage.tier_for(employees=None, source=None) == triage.TIER_LOW_PRIOR


def test_band_edges_are_tier_one():
    from regional import triage
    assert triage.tier_for(employees=100, source="osm") == triage.TIER_READY
    assert triage.tier_for(employees=4000, source="osm") == triage.TIER_READY
    assert triage.tier_for(employees=99, source="osm") == triage.TIER_OUT_OF_BAND
    assert triage.tier_for(employees=4001, source="osm") == triage.TIER_OUT_OF_BAND
