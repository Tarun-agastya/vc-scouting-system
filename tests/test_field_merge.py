"""
Field-level merge, and the undo that is the precondition for offering it.

The old merge could only fill fields that were EMPTY. Crude, but it carried an
accidental safety property — it was incapable of destroying anything you
already had, which is why nobody ever needed to undo one. Choosing per field
which value wins removes exactly that property, so these tests are mostly
about the way back.
"""
import uuid

import pytest

from database.connection import SessionLocal
from database.models import MergeSnapshot, Startup
from processing.field_merge import build_merge_preview, merge_records, undo_merge

PREFIX = "PYTEST-MERGE"


@pytest.fixture
def pair():
    def purge():
        db = SessionLocal()
        try:
            ids = [s.id for s in db.query(Startup).filter(Startup.name.like(f"{PREFIX}%")).all()]
            db.query(MergeSnapshot).filter(
                MergeSnapshot.keeper_name.like(f"{PREFIX}%")).delete(synchronize_session=False)
            for i in ids:
                db.query(Startup).filter(Startup.id == i).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    purge()
    db = SessionLocal()
    keeper_id, loser_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.add_all([
        Startup(id=keeper_id, name=f"{PREFIX} Keeper", normalized_name=f"{PREFIX} keeper".lower(),
                website="https://keeper.example", city="Munich", country="Germany",
                description="keeper text", industry="Robotics",
                source="pytest", source_url="https://pytest/keeper"),
        Startup(id=loser_id, name=f"{PREFIX} Loser", normalized_name=f"{PREFIX} loser".lower(),
                website="https://loser.example", city="Augsburg", country="Germany",
                description="much richer loser text", industry="Robotics",
                employee_count="25", source="pytest", source_url="https://pytest/loser"),
    ])
    db.commit()
    yield db, keeper_id, loser_id
    db.close()
    purge()


def _get(db, sid):
    db.expire_all()
    return db.query(Startup).filter(Startup.id == sid).first()


def test_preview_marks_only_the_real_conflicts(pair):
    db, k_id, l_id = pair
    fields = {f["field"]: f for f in build_merge_preview(_get(db, k_id), _get(db, l_id))}
    assert fields["description"]["differs"] is True
    assert fields["industry"]["differs"] is False, "identical values are not a decision"
    # an empty keeper field defaults to taking the incoming value
    assert fields["employee_count"]["default"] == "incoming"


def test_a_chosen_field_replaces_a_populated_value(pair):
    """The capability the old merge did not have, and the reason undo exists."""
    db, k_id, l_id = pair
    merge_records(db, _get(db, k_id), _get(db, l_id),
                  {"description": "incoming", "city": "keeper"})
    keeper = _get(db, k_id)
    assert keeper.description == "much richer loser text"
    assert keeper.city == "Munich", "a field left on 'keeper' must not move"
    assert _get(db, l_id) is None, "the merged-away record is deleted"


def test_undo_restores_the_record_with_the_same_id(pair):
    """
    Ids are derived from name+website rather than random, so undo is a real
    reversal — every reference to the restored record still resolves.
    """
    db, k_id, l_id = pair
    res = merge_records(db, _get(db, k_id), _get(db, l_id), {"description": "incoming"})
    assert _get(db, l_id) is None

    out = undo_merge(db, res["snapshot_id"])
    assert out["status"] == "undone"
    restored = _get(db, l_id)
    assert restored is not None and restored.name == f"{PREFIX} Loser"
    assert _get(db, k_id).description == "keeper text", "keeper field rolled back"


def test_undo_only_reverts_the_fields_the_merge_touched(pair):
    """
    Snapshotting the whole keeper would make undo destructive in its own
    right, silently rolling back an unrelated edit made afterwards.
    """
    db, k_id, l_id = pair
    res = merge_records(db, _get(db, k_id), _get(db, l_id), {"description": "incoming"})

    keeper = _get(db, k_id)
    keeper.funding_stage = "Series B"      # an edit made AFTER the merge
    db.commit()

    undo_merge(db, res["snapshot_id"])
    keeper = _get(db, k_id)
    assert keeper.description == "keeper text", "the merged field is reverted"
    assert keeper.funding_stage == "Series B", "the later, unrelated edit survives"


def test_a_merge_cannot_happen_without_a_snapshot(pair):
    db, k_id, l_id = pair
    res = merge_records(db, _get(db, k_id), _get(db, l_id), {"description": "incoming"})
    snap = db.query(MergeSnapshot).filter(
        MergeSnapshot.id == res["snapshot_id"]).first()
    assert snap is not None
    assert snap.loser_row.get("name") == f"{PREFIX} Loser", "the whole row is kept"
    assert "description" in (snap.keeper_before or {}), "prior keeper values are kept"


def test_undoing_twice_is_refused(pair):
    db, k_id, l_id = pair
    res = merge_records(db, _get(db, k_id), _get(db, l_id), {"description": "incoming"})
    assert undo_merge(db, res["snapshot_id"])["status"] == "undone"
    assert undo_merge(db, res["snapshot_id"])["status"] == "already_undone"
