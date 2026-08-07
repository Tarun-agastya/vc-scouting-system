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


# ── RC-2b: location-aware matching ──────────────────────────────────────────
# Name alone cannot decide identity here. These two pairs are structurally
# identical as strings but have opposite answers, which is why location had to
# become part of the decision:
#     "Urban"  vs "Urban Maschinenbau"           SAME      (both Memmingen)
#     "Schmid" vs "Hubert Schmid Bauunternehmen" DIFFERENT (different towns)

def test_same_town_merges_name_variants_of_one_company():
    """All four were real duplicate ROWS in the register on 7 Aug."""
    for a, b, city in [
        ("3M Ceramics", "3M Technical Ceramics", "Kempten"),
        ("Autohaus Seitz", "Seitz-Gruppe", "Kempten"),
        ("Kolb Wellpappe GmbH", "Hans Kolb Wellpappe", "Memmingen"),
        ("Urban GmbH", "Urban Maschinenbau", "Memmingen"),
        ("HAWE", "HAWE Hydraulik", "Kaufbeuren"),
        ("Sensortechnik Wiedemann", "Sensor-Technik Wiedemann GmbH", "Kaufbeuren"),
        ("Franz Habisreutinger", "Habisreutinger", "Weingarten"),
    ]:
        assert matcher.is_same(a, b, a_city=city, b_city=city), f"{a} ~ {b}"


def test_different_towns_keep_a_shared_surname_apart():
    assert not matcher.is_same("Schmid GmbH", "Hubert Schmid Bauunternehmen",
                               a_city="Weiler-Simmerberg", b_city="Marktoberdorf")


def test_shared_generic_words_never_merge_even_in_one_town():
    """The 220-pair disaster: an early version merged on ANY shared 4+ char
    word, and German company names are built from a small pool of generic
    ones. Every pair below is two unrelated companies in the same town."""
    for a, b, city in [
        ("Allgäuer Zeitungsverlag GmbH", "Allgäuer Überlandwerk GmbH", "Kempten"),
        ("Schreinerei Thomas Baumgartner", "Schreinerei Hermann Rogg", "Memmingen"),
        ("Amnesty International, Büro Ulm", "Maschuthi, Büro für Gestaltung", "Ulm"),
        ("Super Team Energiesysteme GmbH", "Schreinerei Junges Team", "Memmingen"),
        ("Bürogebäude Schäfer-Technik", "Greif-Technik-Schmid GmbH", "Ulm"),
        ("Andreas Ritzl Maschinenbau GmbH", "Metzner Maschinenbau GmbH", "Ulm"),
        ("Allgäu Mail", "Mona Allgäu", "Kempten"),
        ("Wenger Engineering GmbH", "euro engineering AG", "Ulm"),
    ]:
        assert not matcher.is_same(a, b, a_city=city, b_city=city), f"{a} ~ {b}"


def test_character_similarity_without_a_shared_word_does_not_merge():
    """The clusters overlap on fuzzy score alone (FingerHaus~Fingerhut 84.2 vs
    Sensortechnik~Sensor-Technik 82.1), so a shared WORD is required."""
    assert not matcher.is_same("FingerHaus", "Fingerhut", a_city="Ulm", b_city="Ulm")
    assert not matcher.is_same("Halle 10", "Halle 11", a_city="Ulm", b_city="Ulm")


def test_location_is_only_consulted_when_known_for_both():
    """Wikipedia candidates carry no location. An absent city must never be
    read as 'different place', or they would be forced apart from everything."""
    assert matcher.is_same("Josef Hebel", "Josef Hebel GmbH & Co. KG")
    assert not matcher.is_same("Schmid GmbH", "Hubert Schmid Bauunternehmen")


def test_nearby_coordinates_count_as_the_same_place():
    """A registered office and its plant are often a few streets apart, and
    geocoding a town name lands everyone on the town centre."""
    assert matcher.is_same("Urban", "Urban Maschinenbau",
                           a_coords=(47.9878, 10.1815), b_coords=(47.9900, 10.1900))
    assert not matcher.is_same("Schmid", "Schmid Recycling",
                               a_coords=(47.9878, 10.1815), b_coords=(48.3705, 10.8978))


def test_building_labels_are_not_companies():
    """OSM names buildings and gates: the register held Halle 2/3/5/9/10/11 as
    six separate 'companies', all from one industrial site."""
    for junk in ("Halle 10", "Tor 3", "Gebäude 2", "Pforte 1", "Lager 4"):
        assert not filters.accept(junk), junk
    # ...without catching a real company that merely starts with such a word.
    assert filters.accept("Halle Präzision GmbH")
    assert filters.accept("Haus des Handwerks Bau GmbH")


# ── RC-3 Lever D: Google Places (the only paid source) ──────────────────────

def test_tiling_covers_the_circle_and_overlaps():
    """A single 50 km query returns at most 20 places and would silently imply
    that is all of them, so the circle is tiled."""
    from regional import google_places as gp
    tiles = gp._tiles(radius_km=50.0, tile_km=4.0)
    assert len(tiles) > 400
    from regional.geocode import MEMMINGEN, haversine_km
    # Every tile centre is inside the catchment (plus its own reach).
    assert all(haversine_km(MEMMINGEN, t) <= 54.0 for t in tiles)
    # The centre itself is covered.
    assert any(haversine_km(MEMMINGEN, t) < 1.0 for t in tiles)


def test_cost_estimate_makes_no_api_calls():
    """This is the only script in the project that spends money, so the
    estimate must be computable before any key exists."""
    from regional import google_places as gp
    est = gp.estimate_sweep(radius_km=50.0, tile_km=4.0)
    assert est["tiles"] > 0
    assert est["estimated_usd"] > 0
    # Finer tiles cost more — the knob is honest about its tradeoff.
    assert gp.estimate_sweep(tile_km=2.0)["min_calls"] > est["min_calls"]


def test_types_target_industry_not_consumer_storefronts():
    from regional import google_places as gp
    assert "manufacturer" in gp.B2B_TYPES and "wholesaler" in gp.B2B_TYPES
    for consumer in ("restaurant", "hair_care", "cafe", "clothing_store"):
        assert consumer not in gp.B2B_TYPES


def test_field_mask_stays_in_the_pro_tier():
    """websiteUri / nationalPhoneNumber would promote every call to the more
    expensive Enterprise tier and change the cost of the whole sweep."""
    from regional import google_places as gp
    assert "websiteUri" not in gp.FIELD_MASK
    assert "nationalPhoneNumber" not in gp.FIELD_MASK
    assert "places.displayName" in gp.FIELD_MASK


def test_google_results_are_low_prior_until_sized():
    """Anyone can register a Business Profile, so an unsized Google hit is no
    more likely to be a real employer than an OSM one."""
    from regional import triage
    assert triage.tier_for(employees=None, source="google_places") == triage.TIER_LOW_PRIOR
    assert triage.tier_for(employees=300, source="google_places") == triage.TIER_READY


# ── RC-3b: OSM footprint as a free size proxy ───────────────────────────────

def test_large_footprint_promotes_an_osm_row_into_the_enrichment_queue():
    """Measured 7 Aug on 344 real buildings: the large end is Grob-Werke
    (405,000 m²), Peri (347,000), Daimler Buses (289,000) — all major
    employers; the small end is 'easy-page werbedesign' (95 m²) and a building
    named 'Büro' (81). Area separates them for free, with no LLM call."""
    from regional import triage
    assert triage.tier_for(employees=None, source="osm",
                           footprint_m2=404_994) == triage.TIER_ENRICH
    assert triage.tier_for(employees=None, source="osm",
                           footprint_m2=95) == triage.TIER_LOW_PRIOR


def test_unknown_footprint_is_not_read_as_small():
    """An OSM node with no mapped building has no area. That is missing
    evidence, not evidence of being tiny."""
    from regional import triage
    assert triage.tier_for(employees=None, source="osm",
                           footprint_m2=None) == triage.TIER_LOW_PRIOR
    # ...and it must never demote a notability-sourced company.
    assert triage.tier_for(employees=None, source="wikidata",
                           footprint_m2=None) == triage.TIER_ENRICH


def test_a_known_headcount_still_outranks_the_proxy():
    """Footprint is a proxy for size; an actual headcount is the real thing."""
    from regional import triage
    assert triage.tier_for(employees=50, source="osm",
                           footprint_m2=400_000) == triage.TIER_OUT_OF_BAND
    assert triage.tier_for(employees=650, source="osm",
                           footprint_m2=95) == triage.TIER_READY


def test_polygon_area_is_right_for_a_known_rectangle():
    """~100m x ~100m near Memmingen should measure ~10,000 m²."""
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "rc_fp", pathlib.Path(__file__).parent.parent / "scripts" / "rc_footprints.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    lat, lon = 47.9878, 10.1815
    dlat = 100 / 110_574.0
    import math
    dlon = 100 / (111_320.0 * math.cos(math.radians(lat)))
    square = [{"lat": lat, "lon": lon}, {"lat": lat, "lon": lon + dlon},
              {"lat": lat + dlat, "lon": lon + dlon}, {"lat": lat + dlat, "lon": lon}]
    assert 9_500 < mod.polygon_area_m2(square) < 10_500
    assert mod.polygon_area_m2([{"lat": lat, "lon": lon}]) == 0.0
