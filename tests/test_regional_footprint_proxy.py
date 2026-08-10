"""
Phase RC-3c — node-to-building footprint proxy.

The whole point of this module is that it NEVER guesses: every test here is
either a case that must match (and the "why" is unambiguous evidence) or a
case that must NOT match (because two buildings, or nothing, competes for the
company's location). No network — match_one() is pure.
"""
from regional.footprint_proxy import (
    UNAMBIGUOUS_RADIUS_M,
    BuildingCandidate,
    match_one,
)

MEMMINGEN = (47.9878, 10.1815)


def _nearby(point, meters_north=0.0, meters_east=0.0):
    """Offset a (lat, lon) point by roughly meters_north/meters_east metres."""
    lat, lon = point
    import math
    dlat = meters_north / 110_574.0
    dlon = meters_east / (111_320.0 * math.cos(math.radians(lat)))
    return (lat + dlat, lon + dlon)


def _building(osm_id, centroid, area_m2, name=None):
    tags = {"building": "yes"}
    if name:
        tags["name"] = name
    return BuildingCandidate(osm_id=osm_id, tags=tags, area_m2=area_m2, centroid=centroid)


# ── the confirmed real case: name match wins regardless of exact distance ──

def test_name_match_is_accepted():
    """Live 10 Aug: 'Wilhelm Fischer Spezialmaschinenfabrik GmbH' (a node) has
    its own building 25m away, independently tagged with the same name."""
    company_point = MEMMINGEN
    building_point = _nearby(MEMMINGEN, meters_north=25)
    candidates = [_building(1, building_point, 4200.0,
                            name="Wilhelm Fischer Spezialmaschinenfabrik GmbH")]

    m = match_one("Wilhelm Fischer Spezialmaschinenfabrik GmbH", company_point, candidates)
    assert m is not None
    assert m.method == "name_match"
    assert m.area_m2 == 4200.0


def test_name_match_uses_the_projects_own_identity_rule():
    """Legal-form variation must still count as the same company — this
    reuses regional.matcher.is_same rather than requiring an exact string."""
    candidates = [_building(1, _nearby(MEMMINGEN, meters_north=10), 2000.0,
                            name="Josef Hebel GmbH & Co. KG")]
    m = match_one("Josef Hebel", MEMMINGEN, candidates)
    assert m is not None and m.method == "name_match"


# ── unambiguous nearest: allowed only with nothing else competing ──────────

def test_single_unnamed_building_within_the_tight_radius_is_accepted():
    candidates = [_building(1, _nearby(MEMMINGEN, meters_north=8), 900.0)]
    m = match_one("Some GmbH", MEMMINGEN, candidates)
    assert m is not None
    assert m.method == "unambiguous_nearest"
    assert m.area_m2 == 900.0


def test_a_second_candidate_anywhere_in_the_search_radius_blocks_the_match():
    """This is the core safety property. A second, unrelated building in the
    wider search radius — even one much farther away than the close one —
    must prevent guessing which one is the real match."""
    close = _building(1, _nearby(MEMMINGEN, meters_north=8), 900.0)
    far_but_still_in_range = _building(2, _nearby(MEMMINGEN, meters_north=35), 50_000.0)
    m = match_one("Some GmbH", MEMMINGEN, [close, far_but_still_in_range])
    assert m is None


def test_building_outside_the_tight_radius_with_nothing_closer_is_rejected():
    """A single candidate that is IN the search radius but NOT within the
    tighter unambiguous radius is not close enough to trust without a name."""
    assert UNAMBIGUOUS_RADIUS_M < 40  # sanity on the fixture below
    only = _building(1, _nearby(MEMMINGEN, meters_north=28), 5000.0)
    m = match_one("Some GmbH", MEMMINGEN, [only])
    assert m is None


def test_nothing_nearby_is_left_unresolved():
    far = _building(1, _nearby(MEMMINGEN, meters_north=500), 3000.0)
    assert match_one("Some GmbH", MEMMINGEN, [far]) is None
    assert match_one("Some GmbH", MEMMINGEN, []) is None


def test_name_match_wins_even_when_a_second_unnamed_candidate_exists():
    """Real evidence (the name) should not be discarded just because an
    unrelated building also happens to be nearby."""
    named = _building(1, _nearby(MEMMINGEN, meters_north=20), 3000.0, name="Acme GmbH")
    other = _building(2, _nearby(MEMMINGEN, meters_north=30), 40_000.0)
    m = match_one("Acme GmbH", MEMMINGEN, [named, other])
    assert m is not None and m.method == "name_match" and m.osm_id == 1


def test_wrong_named_building_nearby_does_not_match_and_does_not_block_correctly():
    """A differently-named building nearby is neither a name match nor does
    it count toward the 'unambiguous nearest' rule succeeding — with two
    candidates present (one wrongly named, one unnamed), neither rule is
    satisfied and the result must be no match."""
    wrong_name = _building(1, _nearby(MEMMINGEN, meters_north=10), 3000.0, name="Other Firm GmbH")
    unnamed = _building(2, _nearby(MEMMINGEN, meters_north=12), 3200.0)
    m = match_one("Some GmbH", MEMMINGEN, [wrong_name, unnamed])
    assert m is None
