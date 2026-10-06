"""
processing/auto_merge.py — the rule (pure) and the merge (integration).

Same isolation approach as tests/test_review_resolver.py: auto_merge_pending
scans the whole pending queue by design, so every call passes master_ids to
stay on test data, and require_backup=False so no test ever depends on (or
touches) real backups. The backup GATE itself is tested separately by
monkeypatching, never by deleting anything.
"""
import processing.backup as backup
from database.models import DuplicateReview, MergeSnapshot, Startup
from processing.auto_merge import auto_merge_pending, same_entity
from processing.deduplicator import normalize_company_name


# ── the rule ────────────────────────────────────────────────────────────────

def test_same_domain_on_both_sides_is_the_same_entity():
    ok, why = same_entity({"name": "Chrono24", "website": "https://www.chrono24.com"},
                          {"name": "Chrono24", "website": "https://chrono24.com/about"})
    assert ok and "same domain" in why


def test_a_domain_on_only_one_side_is_not_evidence():
    """Measured 29 Sep: 104 of 151 eligible pairs qualified on exactly this —
    name plus a one-sided domain plus coarse agreement, i.e. name alone. A
    domain says nothing about a record that has no website: two unrelated
    'Bird' companies, one with a site, must not merge."""
    ok, why = same_entity({"name": "Bird", "website": "https://www.messagebird.com",
                           "industry": "B2B SaaS", "country": "Netherlands"},
                          {"name": "Bird", "website": "", "industry": "B2B SaaS",
                           "country": "Netherlands"})
    assert not ok and "no shared domain, page, city or founded year" in why


def test_one_sided_domain_plus_matching_city_is_enough():
    ok, _ = same_entity({"name": "Bird", "website": "https://bird.io", "city": "Munich"},
                        {"name": "Bird", "website": "", "city": "München"})
    assert ok


def test_a_record_flagged_by_verification_is_left_to_a_person():
    """Identity is decided from stored data; a flagged record is the one whose
    stored data we trust least."""
    a = {"name": "Acme", "website": "https://acme.io", "city": "Munich"}
    b = {"name": "Acme", "website": "https://acme.io", "city": "Munich",
         "verification_status": "flagged"}
    ok, why = same_entity(a, b)
    assert not ok and "flagged" in why
    assert same_entity(a, {**b, "verification_status": "verified"})[0]


def test_two_real_domains_stay_with_a_human():
    """BMW: bmwstartupgarage.com vs bmwgroup.com — the shape of 'same name,
    different company'."""
    ok, why = same_entity({"name": "BMW", "website": "https://bmwstartupgarage.com"},
                          {"name": "BMW", "website": "https://www.bmwgroup.com"})
    assert not ok and "two real domains" in why


def test_conflicting_city_vetoes_but_locale_variants_do_not():
    a = {"name": "Acme", "website": "https://acme.io", "city": "Munich"}
    assert not same_entity(a, {"name": "Acme", "website": "", "city": "Hamburg"})[0]
    assert same_entity(a, {"name": "Acme", "website": "", "city": "München"})[0]


def test_listing_site_is_not_a_domain():
    """Reformer Club's 'website' was munich-startup.de, the article it was found
    on. That must not count as a second real domain."""
    ok, _ = same_entity({"name": "Reformer Club", "website": "https://www.munich-startup.de/x",
                         "city": "Munich"},
                        {"name": "Reformer Club", "website": "https://reformerclub.de",
                         "city": "Munich"})
    assert ok


def test_two_bare_stubs_prove_nothing():
    """No domain either side and nothing specific in common: 'Nova' twice.
    Industry/country agreement is not enough — they are coarse."""
    a = {"name": "Nova", "industry": "B2B SaaS", "country": "Germany"}
    b = {"name": "Nova", "industry": "B2B SaaS", "country": "Germany"}
    ok, why = same_entity(a, b)
    assert not ok and "no shared domain, page, city or founded year" in why


def test_no_domain_but_matching_city_is_enough():
    assert same_entity({"name": "Nova", "city": "Munich"}, {"name": "Nova", "city": "München"})[0]


def test_different_names_never_qualify():
    assert not same_entity({"name": "Foo GmbH"}, {"name": "Bar GmbH"})[0]


# ── the merge ───────────────────────────────────────────────────────────────

def _pair(make, db, tag):
    """Two real rows that are the same entity, plus the review pointing at them.
    make() would route a same-name second sighting to no_op (A1), so the
    duplicate is created under a different name and renamed afterwards."""
    keeper_id, _ = make(f"AM {tag} Keep", website=f"pytest-am-{tag}.com", city="Munich",
                        description="widget maker")
    loser_id, _ = make(f"AM {tag} Lose", city="Munich", description="widget maker",
                       funding_stage="Seed")
    loser = db.query(Startup).filter(Startup.id == loser_id).first()
    keeper = db.query(Startup).filter(Startup.id == keeper_id).first()
    loser.name = keeper.name
    loser.normalized_name = normalize_company_name(keeper.name)
    loser.fingerprint = None
    rev = DuplicateReview(
        review_type="possible_duplicate", master_id=keeper_id, master_name=keeper.name,
        incoming_id=loser_id, incoming_name=keeper.name, risk_level="low",
        status="pending", source="pytest")
    db.add(rev)
    db.commit()
    return keeper_id, loser_id, rev.id


def test_merge_deletes_the_loser_snapshots_and_undo_restores_it(make, db):
    from processing.field_merge import undo_merge

    keeper_id, loser_id, review_id = _pair(make, db, "one")
    stats = auto_merge_pending(50, apply=True, master_ids=[keeper_id], require_backup=False)
    snap_id = None
    try:
        assert stats["merged"] == 1 and stats["failed"] == 0

        db.expire_all()
        assert db.query(Startup).filter(Startup.id == loser_id).first() is None
        keeper = db.query(Startup).filter(Startup.id == keeper_id).first()
        assert keeper.funding_stage == "Seed"            # took the value it lacked

        # Every review for the pair is closed. Which one carries the audit
        # entry depends on which the job reached first — the matcher may have
        # staged its own review when the stub was created (DZ.S had three) —
        # so find the merge by its snapshot, not by a specific review id.
        reviews = db.query(DuplicateReview).filter(DuplicateReview.master_id == keeper_id).all()
        assert reviews and all(r.status == "approved" for r in reviews)
        audited = [r for r in reviews if "auto_merge" in (r.evidence or {})]
        assert len(audited) == 1
        snap_id = audited[0].evidence["auto_merge"]["snapshot_id"]   # auditable + undoable
        assert db.query(MergeSnapshot).filter(MergeSnapshot.id == snap_id).first()

        # Undo brings the deleted record back under its ORIGINAL id.
        assert undo_merge(db, snap_id)["status"] == "undone"
        db.expire_all()
        assert db.query(Startup).filter(Startup.id == loser_id).first() is not None
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == keeper_id).delete()
        db.commit()


def test_dry_run_changes_nothing(make, db):
    keeper_id, loser_id, review_id = _pair(make, db, "dry")
    stats = auto_merge_pending(50, apply=False, master_ids=[keeper_id])
    assert stats["eligible"] == 1 and stats["merged"] == 0
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == loser_id).first() is not None
    assert db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first().status == "pending"


def test_no_fresh_backup_blocks_every_merge(make, db, monkeypatch):
    """Undo covers a bad merge, not a bad disk. No backup, no merging."""
    keeper_id, loser_id, _ = _pair(make, db, "gate")
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: None)
    stats = auto_merge_pending(50, apply=True, master_ids=[keeper_id])   # gate ON
    assert "no complete backup" in stats.get("blocked", "")
    assert stats["merged"] == 0
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == loser_id).first() is not None


def test_a_stale_backup_also_blocks(make, db, monkeypatch):
    keeper_id, loser_id, _ = _pair(make, db, "stale")
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: 80.0)
    stats = auto_merge_pending(50, apply=True, master_ids=[keeper_id])
    assert "80.0h old" in stats.get("blocked", "") and stats["merged"] == 0


def test_other_reviews_naming_the_loser_are_not_stranded(make, db):
    """DZ.S had three reviews for one pair. After the merge the extras must be
    closed, and a review naming the loser against a THIRD record must be
    repointed at the keeper — not left pointing at a deleted row."""
    keeper_id, loser_id, _ = _pair(make, db, "moot")
    third_id, _ = make("AM moot Third", website="pytest-am-moot-third.com", city="Berlin",
                       description="something else")
    same_pair = DuplicateReview(
        review_type="possible_duplicate", master_id=keeper_id, master_name="PYTEST AM moot Keep",
        incoming_id=loser_id, incoming_name="x", risk_level="low", status="pending", source="pytest")
    other = DuplicateReview(
        review_type="possible_duplicate", master_id=third_id, master_name="PYTEST AM moot Third",
        incoming_id=loser_id, incoming_name="x", risk_level="low", status="pending", source="pytest")
    db.add_all([same_pair, other])
    db.commit()
    sp_id, ot_id = same_pair.id, other.id

    try:
        auto_merge_pending(50, apply=True, master_ids=[keeper_id], require_backup=False)
        db.expire_all()
        assert db.query(DuplicateReview).get(sp_id).status == "approved"       # moot
        assert str(db.query(DuplicateReview).get(ot_id).incoming_id) == str(keeper_id)   # repointed
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == keeper_id).delete()
        db.commit()


# ── provenance: the same source page ────────────────────────────────────────

_PAGE = "https://schwaben.digital/startups"


def _rec(**kw):
    return {"name": "Qbilon", **kw}


def test_identical_name_from_the_same_page_is_a_recrawl_not_two_companies():
    """A listing doesn't list two different companies under one identical
    name. 179 of the 183 identical-name pairs held back by the stricter rule
    shared a page — and a re-crawl of that page is why they kept regrowing."""
    ok, why = same_entity(_rec(source_history=[{"url": _PAGE}]),
                          _rec(source_url=_PAGE + "/"))          # trailing slash tolerated
    assert ok and "source page" in why


def test_a_shared_page_never_overrides_a_real_contradiction():
    a = _rec(source_url=_PAGE, city="Munich", website="https://qbilon.io")
    assert not same_entity(a, _rec(source_url=_PAGE, city="Hamburg"))[0]
    assert not same_entity(a, _rec(source_url=_PAGE, website="https://other.com"))[0]
    assert not same_entity(_rec(source_url=_PAGE, founded_year=2019),
                           _rec(source_url=_PAGE, founded_year=2021))[0]


def test_different_pages_and_nothing_else_is_still_name_alone():
    ok, why = same_entity(_rec(source_url="https://a.example/x"), _rec(source_url="https://b.example/y"))
    assert not ok and "no shared domain, page, city or founded year" in why


def test_flagged_veto_applies_to_stored_field_evidence_but_not_to_provenance():
    """Identity read from a domain or city inherits the doubt about a flagged
    record; identity read from 'both came from this page' does not."""
    flagged = {"verification_status": "flagged"}
    on_fields = same_entity(_rec(website="https://qbilon.io", **flagged),
                            _rec(website="https://qbilon.io"))
    assert not on_fields[0] and "flagged" in on_fields[1]
    on_page = same_entity(_rec(source_url=_PAGE, **flagged), _rec(source_url=_PAGE))
    assert on_page[0]


def test_industry_noise_is_ignored_only_when_the_page_is_shared():
    """Industry is a coarse label a classifier assigns per copy. Two copies of
    one page's record disagreeing on it is noise; two unrelated records
    disagreeing on it is a real signal."""
    a = _rec(industry="Energy & CleanTech", website="https://qbilon.io")
    b = _rec(industry="B2B SaaS", website="https://qbilon.io")
    assert not same_entity(a, b)[0]                                        # no shared page: veto
    assert same_entity({**a, "source_url": _PAGE}, {**b, "source_url": _PAGE})[0]


def test_a_record_is_never_both_keeper_and_loser_in_one_batch(make, db):
    """
    Found on the first real run (30 Sep): three copies of one company gave
    pairs (A,B) and (B,C). B was merged into A and deleted, then used as the
    KEEPER of the second pair — merging C into a record that no longer
    existed. The snapshot recovered it, but it must never be attempted.
    """
    def copy(tag):
        rid, _ = make(f"AM chain {tag}", website="pytest-am-chain.com", city="Munich",
                      description="widget maker")
        return rid
    a, b, c = copy("a"), copy("b"), copy("c")
    for rid in (b, c):
        row = db.query(Startup).filter(Startup.id == rid).first()
        row.name = "PYTEST AM chain a"
        row.normalized_name = normalize_company_name("PYTEST AM chain a")
        row.fingerprint = None
    db.add_all([
        DuplicateReview(review_type="possible_duplicate", master_id=a, master_name="x",
                        incoming_id=b, incoming_name="x", risk_level="low", status="pending", source="pytest"),
        DuplicateReview(review_type="possible_duplicate", master_id=b, master_name="x",
                        incoming_id=c, incoming_name="x", risk_level="low", status="pending", source="pytest"),
    ])
    db.commit()

    try:
        first = auto_merge_pending(50, apply=True, master_ids=[a, b], require_backup=False)
        assert first["failed"] == 0
        assert first["merged"] == 1                       # the chained pair waited
        db.expire_all()
        survivors = {r for r in (a, b, c) if db.query(Startup).filter(Startup.id == r).first()}
        assert a in survivors and len(survivors) == 2     # exactly one copy merged away

        # Next run picks up the repointed review and finishes the job — nothing lost.
        second = auto_merge_pending(50, apply=True, master_ids=[a], require_backup=False)
        assert second["failed"] == 0
        db.expire_all()
        assert [r for r in (a, b, c) if db.query(Startup).filter(Startup.id == r).first()] == [a]
    finally:
        db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id.in_([a, b])).delete(
            synchronize_session=False)
        db.commit()
