"""
The Browse "Deduplicate" button — processing/dedup_run.py and POST /dedup/run.

Every call is scoped to test record ids (`ids=[...]`) and stubs the backup
step. run(ids=None, apply=True) scans and merges the WHOLE live database, so
no test here ever does that — an earlier test in this project merged 10 live
pairs by exactly that mistake.
"""
import asyncio

import pytest

import processing.dedup_run as dedup_run
from database.models import DuplicateReview, MergeSnapshot, Startup, SuppressedMatch
from processing.deduplicator import normalize_company_name

PAGE = "https://pytest.example/dedup-run"


@pytest.fixture(autouse=True)
def _no_real_backup(monkeypatch):
    monkeypatch.setattr(dedup_run, "_ensure_backup", lambda: {"ok": True, "note": "stubbed"})


def _copies(make, db, tag, n=2, page=PAGE):
    """n records with one identical name from one page; the first is oldest."""
    first, _ = make(f"DR {tag} 0", website=f"pytest-dr-{tag}.com", city="Munich",
                    description="widget maker", source_url=page + tag)
    ids = [first]
    name = db.query(Startup).filter(Startup.id == first).first().name
    for i in range(1, n):
        rid, _ = make(f"DR {tag} {i}", city="Munich", description="widget maker",
                      source_url=page + tag)
        row = db.query(Startup).filter(Startup.id == rid).first()
        row.name, row.normalized_name, row.fingerprint = name, normalize_company_name(name), None
        ids.append(rid)
    db.commit()
    return ids


def _cleanup(db, ids):
    db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id.in_(ids)).delete(synchronize_session=False)
    db.commit()


def test_dry_run_reports_and_changes_nothing(make, db):
    ids = _copies(make, db, "dry")
    r = dedup_run.run(ids, apply=False)
    assert r["dry_run"] is True and r["merge_count"] == 1
    assert r["merge_preview"][0]["rule"]
    db.expire_all()
    assert db.query(Startup).filter(Startup.id.in_(ids)).count() == 2


def test_selected_scope_only_looks_at_duplicates_of_what_was_selected(make, db):
    """A group with no selected member is not touched."""
    a = _copies(make, db, "sel-a")
    b = _copies(make, db, "sel-b")
    r = dedup_run.run([a[0]], apply=False)
    assert r["scope"] == "selected" and r["merge_count"] == 1
    assert {x["keeper"] for x in r["merge_preview"]} == {db.query(Startup).get(a[0]).name}


def test_apply_merges_into_the_oldest_and_leaves_an_undoable_snapshot(make, db):
    ids = _copies(make, db, "apply", n=3)                  # three copies: the chain shape
    try:
        r = dedup_run.run(ids, apply=True)
        assert r["merged"] == 2 and r["failed"] == 0
        db.expire_all()
        survivors = [i for i in ids if db.query(Startup).filter(Startup.id == i).first()]
        assert survivors == [ids[0]]                       # the OLDEST survives
        snaps = db.query(MergeSnapshot).filter(MergeSnapshot.keeper_id == ids[0]).all()
        assert len(snaps) == 2 and all(s.loser_row for s in snaps)
        review = db.query(DuplicateReview).filter(DuplicateReview.master_id == ids[0]).first()
        assert review.status == "approved" and review.evidence["auto_merge"]["via"] == "dedup-button"
    finally:
        _cleanup(db, ids)


def test_a_pair_a_person_marked_different_is_never_merged(make, db):
    ids = _copies(make, db, "diff")
    db.add(SuppressedMatch(kind="known_different", master_id=ids[0], other_id=ids[1]))
    db.commit()
    r = dedup_run.run(ids, apply=True)
    assert r["merge_count"] == 0 and r["held"].get("a person marked these different") == 1
    db.expire_all()
    assert db.query(Startup).filter(Startup.id.in_(ids)).count() == 2


def test_the_rule_still_refuses_two_real_domains(make, db):
    a, _ = make("DR twodom 0", website="pytest-dr-twodom-a.com", city="Munich",
                description="widget maker", source_url=PAGE + "twodom")
    b, _ = make("DR twodom 1", website="pytest-dr-twodom-b.com", city="Munich",
                description="widget maker", source_url=PAGE + "twodom")
    name = db.query(Startup).filter(Startup.id == a).first().name
    row = db.query(Startup).filter(Startup.id == b).first()
    row.name, row.normalized_name, row.fingerprint = name, normalize_company_name(name), None
    db.commit()
    r = dedup_run.run([a, b], apply=True)
    assert r["merge_count"] == 0 and "two real domains" in r["held"]
    db.expire_all()
    assert db.query(Startup).filter(Startup.id.in_([a, b])).count() == 2


def test_a_failed_backup_blocks_the_run_and_merges_nothing(make, db, monkeypatch):
    monkeypatch.setattr(dedup_run, "_ensure_backup", lambda: {"ok": False, "note": "disk full"})
    ids = _copies(make, db, "nobackup")
    r = dedup_run.run(ids, apply=True)
    assert r["blocked"] == "disk full" and r.get("merged", 0) == 0
    db.expire_all()
    assert db.query(Startup).filter(Startup.id.in_(ids)).count() == 2


def test_held_and_near_pairs_are_only_queued_when_asked(make, db):
    a, _ = make("DR near Acme Robotics", website="pytest-dr-near-a.com", city="Munich",
                description="robots", source_url=PAGE + "near")
    b, _ = make("DR near Acme Robotix", website="pytest-dr-near-b.com", city="Berlin",
                description="robots", source_url=PAGE + "near2")
    for rid in (a, b):                                    # ignore anything the matcher staged itself
        db.query(DuplicateReview).filter(DuplicateReview.master_id == rid).delete()
    db.commit()

    plain = dedup_run.run([a, b], apply=True, include_review=False)
    assert plain["near_count"] >= 1 and "staged_reviews" not in plain
    db.expire_all()
    assert db.query(DuplicateReview).filter(DuplicateReview.source == "dedup_run").count() == 0

    asked = dedup_run.run([a, b], apply=True, include_review=True)
    assert asked["staged_reviews"] >= 1
    db.expire_all()
    assert db.query(DuplicateReview).filter(DuplicateReview.source == "dedup_run").count() >= 1
    assert db.query(Startup).filter(Startup.id.in_([a, b])).count() == 2      # near names are NEVER merged
    # asking twice must not stack duplicate reviews
    again = dedup_run.run([a, b], apply=True, include_review=True)
    assert again["staged_reviews"] == 0


def test_limit_caps_merges_and_reports_what_is_left(make, db):
    ids = _copies(make, db, "limit", n=4)
    try:
        r = dedup_run.run(ids, apply=True, limit=1)
        assert r["merged"] == 1 and r["remaining"] == 2
    finally:
        _cleanup(db, ids)


def test_a_second_concurrent_run_is_refused(make, db):
    ids = _copies(make, db, "busy")
    assert dedup_run._lock.acquire(blocking=False)
    try:
        assert dedup_run.run(ids, apply=False).get("busy") is True
    finally:
        dedup_run._lock.release()


# ── the endpoint ────────────────────────────────────────────────────────────

def test_endpoint_defaults_to_a_dry_run_and_rejects_an_empty_selection(make, db):
    from fastapi import HTTPException

    from api.routes import dedup as D
    ids = _copies(make, db, "api")
    out = asyncio.run(D.run_dedup(D.DedupRunRequest(ids=ids)))          # dry_run defaults True
    assert out["dry_run"] is True and out["merge_count"] == 1
    with pytest.raises(HTTPException) as e:
        asyncio.run(D.run_dedup(D.DedupRunRequest(ids=[])))
    assert e.value.status_code == 400
