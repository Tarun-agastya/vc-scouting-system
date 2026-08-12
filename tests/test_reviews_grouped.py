"""
Phase Y (5 Aug) — one row per startup in the Review Inbox.

Motivated live: 1,090 pending field_update reviews covered only 355
distinct startups; 223 startups had more than one (Bliro 11x, Omegga and
Alqem 18x each) because each re-crawl of the same source page staged its
own reworded proposal. These tests cover the three pieces: Y-3 (stop the
growth at creation), Y-1 (group the existing backlog for display), Y-2
(resolve a whole group in one action).
"""
import asyncio
from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from api.routes import reviews as R


def _pending_field_updates(db, master_id):
    return db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == "field_update",
        DuplicateReview.status == "pending",
    ).all()


# ── Y-3: per-field coverage stops the growth ──────────────────────────────

def test_reworded_proposal_does_not_create_a_new_row(make, db):
    """The Blickfeld/Bliro shape, updated for Phase Z (12 Aug): description
    itself no longer stages ANY review (it's resolved deterministically by
    field_policy.better_freetext — see tests/test_field_policy.py and
    tests/test_web_verifier.py for that), and a genuinely EMPTY field now
    auto-applies rather than stages (Z-3). This test exercises the same Y-3
    per-field coverage logic on a real, still-staged CONFLICT instead: a
    populated field, contradicted the same way by two separate runs, must
    not stage a second row for the second run."""
    rid, _ = make("Grp Reword", website="pytest-grp-reword.com", city="Munich",
                  funding_stage="Pre-Seed")
    make("Grp Reword", website="pytest-grp-reword.com", city="Munich",
         funding_stage="Seed")
    first_count = len(_pending_field_updates(db, rid))
    assert first_count >= 1

    # Same contradicting value, a later independent run — must NOT add a
    # second row for `funding_stage`.
    make("Grp Reword", website="pytest-grp-reword.com", city="Munich",
         funding_stage="Seed")
    assert len(_pending_field_updates(db, rid)) == first_count


def test_materially_different_value_still_creates_a_row(make, db):
    """A genuinely new fact must still get its own review — coverage is
    per-value, not a blanket 'this startup already has a pending review'."""
    rid, _ = make("Grp NewFact", website="pytest-grp-newfact.com", city="Munich",
                  description="Widget factory.")
    make("Grp NewFact", website="pytest-grp-newfact.com", city="Munich",
         description="Munich widget factory, family owned since 1990.")
    before = len(_pending_field_updates(db, rid))

    # A completely different city is not "covered" by the pending description proposal.
    make("Grp NewFact", website="pytest-grp-newfact.com", city="Berlin",
         description="Widget factory.")
    reviews = _pending_field_updates(db, rid)
    assert len(reviews) > before
    assert any("city" in (r.proposed_changes or {}) for r in reviews)


def test_partial_field_set_only_new_field_survives(make, db):
    """Run A proposes {city, funding_stage} (both real conflicts — populated
    old values); run B proposes {funding_stage} alone with the SAME value,
    no city this time — must add nothing (old whole-dict equality would
    have staged a second row here since the field sets differ, even though
    the substance doesn't)."""
    rid, _ = make("Grp Partial", website="pytest-grp-partial.com", city="Munich",
                  funding_stage="Pre-Seed")
    make("Grp Partial", website="pytest-grp-partial.com", city="Hamburg",
         funding_stage="Seed")
    count_after_first = len(_pending_field_updates(db, rid))
    assert count_after_first >= 1

    # Same funding_stage value already pending; no city this time.
    make("Grp Partial", website="pytest-grp-partial.com", funding_stage="Seed")
    assert len(_pending_field_updates(db, rid)) == count_after_first


# ── Y-1: grouped listing ──────────────────────────────────────────────────

def test_grouped_endpoint_collapses_to_one_entry(make, db):
    rid, _ = make("Grp List", website="pytest-grp-list.com", city="Munich",
                  description="Fact one.")
    make("Grp List", website="pytest-grp-list.com", city="Hamburg", description="Fact one.")
    make("Grp List", website="pytest-grp-list.com", city="Berlin", description="Fact one.")
    members = _pending_field_updates(db, rid)
    assert len(members) >= 2  # two distinct `city` values -> two rows, both on one master

    result = asyncio.run(R.list_reviews_grouped(status="pending", db=SessionLocal()))
    matches = [g for g in result["groups"] if g["master_id"] == str(rid)]
    assert len(matches) == 1
    group = matches[0]
    assert group["review_count"] == len(members)
    assert set(group["review_ids"]) == {str(r.id) for r in members}


def test_grouped_candidates_are_deduped_by_exact_value(make, db):
    """Two reviews proposing the exact same city value collapse into one
    candidate with count=2, not two separate candidates."""
    rid, _ = make("Grp Dedupe", website="pytest-grp-dedupe.com", city="Munich",
                  description="X")
    make("Grp Dedupe", website="pytest-grp-dedupe.com", city="Hamburg", description="X")
    # Force a second, independent review proposing the SAME new city value
    # by rejecting the first (so it's no longer "pending"/covering) then
    # re-proposing — simpler: directly craft a second DuplicateReview row.
    from datetime import datetime
    dup = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Grp Dedupe",
        proposed_changes={"city": {"old": "Munich", "new": "Hamburg",
                                    "incoming_source": "test", "incoming_extracted_at": "2026-01-01"}},
        risk_level="high", status="pending", created_at=datetime.utcnow(),
    )
    db.add(dup)
    db.commit()

    result = asyncio.run(R.list_reviews_grouped(status="pending", db=SessionLocal()))
    group = next(g for g in result["groups"] if g["master_id"] == str(rid))
    city_candidates = group["fields"]["city"]
    hamburg = [c for c in city_candidates if c["value"] == "Hamburg"]
    assert len(hamburg) == 1
    assert hamburg[0]["count"] == 2
    assert len(hamburg[0]["review_ids"]) == 2


def test_grouped_current_reads_live_master_not_stale_old(make, db):
    """`current` must reflect the LIVE master value, never a review's own
    (possibly stale) `old` snapshot."""
    rid, _ = make("Grp Current", website="pytest-grp-current.com", city="Munich",
                  description="X")
    make("Grp Current", website="pytest-grp-current.com", city="Hamburg", description="X")

    # Mutate the master directly, bypassing the review flow entirely —
    # simulates the master having moved on since the review was staged.
    m = db.query(Startup).filter(Startup.id == rid).first()
    m.city = "Berlin"
    db.commit()

    result = asyncio.run(R.list_reviews_grouped(status="pending", db=SessionLocal()))
    group = next(g for g in result["groups"] if g["master_id"] == str(rid))
    assert group["current"]["city"] == "Berlin"


def test_possible_duplicate_reviews_are_not_grouped(make, db):
    """Grouping is field_update only — a possible_duplicate is a genuine
    distinct pair and must never be silently folded into a group view."""
    r1, _ = make("Grp Dup A", city="Munich", description="widget maker one")
    r2, s2 = make("Grp Dup A almost", city="Munich", description="widget maker one")
    result = asyncio.run(R.list_reviews_grouped(status="pending", db=SessionLocal()))
    for g in result["groups"]:
        for r in db.query(DuplicateReview).filter(DuplicateReview.id.in_(g["review_ids"])).all():
            assert r.review_type == "field_update"


# ── Y-2: resolve a whole group in one action ──────────────────────────────

def test_resolve_applies_picks_and_reindexes_once(make, db, monkeypatch):
    rid, _ = make("Grp Resolve", website="pytest-grp-resolve.com", city="Munich",
                  description="Fact one.")
    make("Grp Resolve", website="pytest-grp-resolve.com", city="Hamburg", description="Fact one.")
    make("Grp Resolve", website="pytest-grp-resolve.com", city="Berlin", description="Fact one.")
    members = _pending_field_updates(db, rid)
    assert len(members) >= 2

    reindex_calls = []
    monkeypatch.setattr(R, "_reindex", lambda db, master: reindex_calls.append(master.id))

    result = asyncio.run(R.resolve_grouped_reviews(
        str(rid), R.GroupResolveRequest(selections={"city": "Hamburg"}), db=SessionLocal(),
    ))
    assert result["status"] == "resolved"
    assert len(reindex_calls) == 1  # ONE reindex for the whole group, not one per review

    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().city == "Hamburg"

    resolved = db.query(DuplicateReview).filter(DuplicateReview.id.in_([str(r.id) for r in members])).all()
    assert all(r.status in ("approved", "rejected") for r in resolved)
    assert not _pending_field_updates(db, rid)  # group fully closed


def test_resolve_rejects_unselected_candidates_with_suppression(make, db, monkeypatch):
    rid, _ = make("Grp Suppress", website="pytest-grp-suppress.com", city="Munich",
                  description="Fact one.")
    make("Grp Suppress", website="pytest-grp-suppress.com", city="Hamburg", description="Fact one.")
    make("Grp Suppress", website="pytest-grp-suppress.com", city="Berlin", description="Fact one.")
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)

    asyncio.run(R.resolve_grouped_reviews(
        str(rid), R.GroupResolveRequest(selections={"city": "Hamburg"}), db=SessionLocal(),
    ))
    sup = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "rejected_value", SuppressedMatch.master_id == rid,
        SuppressedMatch.field == "city", SuppressedMatch.value == "Berlin",
    ).first()
    assert sup is not None


def test_resolve_with_no_selection_rejects_all_and_applies_nothing(make, db, monkeypatch):
    rid, _ = make("Grp NoSelect", website="pytest-grp-noselect.com", city="Munich",
                  description="Fact one.")
    make("Grp NoSelect", website="pytest-grp-noselect.com", city="Hamburg", description="Fact one.")
    monkeypatch.setattr(R, "_reindex", lambda db, master: None)

    asyncio.run(R.resolve_grouped_reviews(
        str(rid), R.GroupResolveRequest(selections={}), db=SessionLocal(),
    ))
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().city == "Munich"  # untouched
    assert not _pending_field_updates(db, rid)  # still closed, all rejected
