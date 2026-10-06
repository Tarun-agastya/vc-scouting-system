"""
The Browse "Deduplicate" button: run duplicate detection now, on everything or
on the records a person selected.

What it does, and what it deliberately does not
-----------------------------------------------
Two kinds of duplicate, handled differently:

  * IDENTICAL normalised name — the mechanical case (the same record seen
    twice). Each such group merges into its OLDEST record, but only for
    members that pass auto_merge.same_entity (same domain both sides, the same
    source page, or a matching city/founded year; never two real domains, a
    conflicting city/year, or a record verification has flagged). Members that
    fail are HELD, with the reason, and are only queued for a person if asked.
  * NEAR names ("Acme Robotics" / "Acme Robotix") — never merged here. They
    are counted, and optionally queued as possible_duplicate reviews.

So a click can shrink the database, but only by pairs the nightly rule would
merge anyway, and every one is snapshotted and undoable (Review Inbox →
Recent merges). Two guards specific to a person pressing the button:

  * a pair a person already marked "different company" is NEVER merged, however
    identical the names (SuppressedMatch known_different);
  * a fresh backup is required, and taken automatically if the last one is
    stale — the click should not fail on housekeeping — but the run refuses if
    that backup cannot be made.

Keeper is always the oldest record in the group: its id stays the canonical
identity (same convention as scripts/dedup_sweep.py), and because the keeper is
fixed for the whole group a record can never be both keeper and loser in one
run — the bug the first real batch found in the review-based path.

Dry-run first. `plan()` writes nothing; `run(apply=True)` acts on that same plan.
"""
import logging
import threading
import uuid
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Two normalised names this similar are "near" (rapidfuzz token_sort_ratio).
# scripts/dedup_sweep.py uses 88; a little stricter here because the result
# can be queued as reviews a person then has to read.
NEAR_THRESHOLD = 90
_PREVIEW_ROWS = 25
_lock = threading.Lock()          # one run at a time: two clicks must not race


def _oldest(records):
    return min(records, key=lambda r: (r.created_at or datetime.min, str(r.id)))


def _pair_blocked(db, a, b) -> bool:
    """A person said these are different companies (or one was already settled)."""
    from processing.storage import _is_known_different
    return _is_known_different(db, a.id, b.id)


def _existing_pending_review(db, a, b):
    from database.models import DuplicateReview
    return (db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending",
        DuplicateReview.review_type.in_(["possible_duplicate", "anomaly"]),
        ((DuplicateReview.master_id == a.id) & (DuplicateReview.incoming_id == b.id)) |
        ((DuplicateReview.master_id == b.id) & (DuplicateReview.incoming_id == a.id))).first())


def plan(db, ids=None, include_review: bool = False) -> dict:
    """
    Find duplicates. Writes nothing.

    ids=None scans every record; a list scans for duplicates OF those records —
    wherever the other copy is, selected or not (a copy you didn't tick is
    still a copy of the one you did).
    """
    from database.models import Startup
    from processing.auto_merge import same_entity

    rows = db.query(Startup).all()
    scope_ids = {str(i) for i in ids} if ids else None
    in_scope = (lambda r: True) if scope_ids is None else (lambda r: str(r.id) in scope_ids)

    by_name = defaultdict(list)
    for r in rows:
        if r.normalized_name:
            by_name[r.normalized_name].append(r)

    merge, held, held_pairs, held_all = [], defaultdict(int), [], []
    for name, members in by_name.items():
        if len(members) < 2 or not any(in_scope(m) for m in members):
            continue
        keeper = _oldest(members)
        for m in members:
            if m.id == keeper.id:
                continue
            if _pair_blocked(db, keeper, m):
                held["a person marked these different"] += 1
                continue
            ok, why = same_entity(keeper, m)
            if ok:
                merge.append((keeper, m, why))
            else:
                held[why.split(" (")[0]] += 1
                held_all.append((keeper, m, why))
                if len(held_pairs) < _PREVIEW_ROWS:
                    held_pairs.append({"keeper": keeper.name, "other": m.name, "why": why})

    near = _near_pairs(db, rows, by_name, in_scope)

    return {
        "scope": "selected" if scope_ids is not None else "all",
        "records_total": len(rows),
        "scope_records": len(scope_ids) if scope_ids is not None else len(rows),
        "merge_count": len(merge),
        "merge_preview": [{"keeper": k.name, "loser": l.name, "rule": w} for k, l, w in merge[:_PREVIEW_ROWS]],
        "held": dict(held),
        "held_preview": held_pairs,
        "near_count": len(near),
        "near_preview": [{"a": a.name, "b": b.name, "score": sc} for a, b, sc in near[:_PREVIEW_ROWS]],
        "_merge": merge, "_near": near, "_held": held_all, "_include_review": include_review,
    }


def _near_pairs(db, rows, by_name, in_scope):
    """Distinct-but-similar names, oldest record of each name. Never merged."""
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return []
    names = sorted(by_name)
    scope_names = sorted(n for n in names if any(in_scope(r) for r in by_name[n]))
    if not scope_names:
        return []
    matrix = process.cdist(scope_names, names, scorer=fuzz.token_sort_ratio,
                           score_cutoff=NEAR_THRESHOLD, workers=-1)
    seen, out = set(), []
    for i, a in enumerate(scope_names):
        for j in matrix[i].nonzero()[0]:
            b = names[j]
            if a == b or (b, a) in seen or (a, b) in seen:
                continue
            seen.add((a, b))
            ra, rb = _oldest(by_name[a]), _oldest(by_name[b])
            if _pair_blocked(db, ra, rb) or _existing_pending_review(db, ra, rb):
                continue
            out.append((ra, rb, float(matrix[i][j])))
    out.sort(key=lambda t: -t[2])
    return out


def _stage_review(db, keeper, other, evidence, confidence) -> bool:
    """Queue one possible_duplicate for a person. Skips a pair already pending."""
    from database.models import DuplicateReview

    if _existing_pending_review(db, keeper, other):
        return False
    db.add(DuplicateReview(
        review_type="possible_duplicate", master_id=keeper.id, master_name=keeper.name,
        incoming_id=other.id, incoming_name=other.name, incoming_data={},
        evidence={**evidence, "dedup_run": True, "evidence_level": "normal"},
        risk_level="low", confidence=confidence, source="dedup_run", status="pending"))
    return True


def _ensure_backup() -> dict:
    """A fresh backup, taken now if the last one is stale."""
    from processing.auto_merge import backup_gate
    from processing.backup import run_backup

    ok, why = backup_gate()
    if ok:
        return {"ok": True, "note": why}
    made = run_backup()
    if "error" in made:
        return {"ok": False, "note": f"backup was stale ({why}) and a new one failed: {made['error']}"}
    ok, why = backup_gate()
    return {"ok": ok, "note": ("took a fresh backup: " if ok else "") + why}


def run(ids=None, *, apply: bool = False, include_review: bool = False, limit: int = 100) -> dict:
    """
    Plan, and if apply, act on the plan. Never raises.

    At most `limit` merges per call — each re-embeds the surviving record — and
    `remaining` says how many are left, so a big database is several clicks
    rather than one request that outlives its own timeout.
    """
    from database.connection import SessionLocal
    from processing.auto_merge import merge_one

    if not _lock.acquire(blocking=False):
        return {"busy": True, "error": "a deduplication run is already in progress"}
    db = SessionLocal()
    try:
        p = plan(db, ids, include_review)
        merge, near, held_all = p.pop("_merge"), p.pop("_near"), p.pop("_held")
        p.pop("_include_review")
        p["dry_run"] = not apply
        if not apply:
            return p

        b = _ensure_backup()
        p["backup"] = b
        if not b["ok"]:
            p["blocked"] = b["note"]
            return p

        stats = {"merged": 0, "failed": 0}
        run_id = uuid.uuid4()
        for keeper, loser, rule in merge[:limit]:
            review = _existing_pending_review(db, keeper, loser)
            if review is None:
                from database.models import DuplicateReview
                review = DuplicateReview(
                    review_type="possible_duplicate", master_id=keeper.id, master_name=keeper.name,
                    incoming_id=loser.id, incoming_name=loser.name, incoming_data={},
                    evidence={"name_similarity": 1.0, "dedup_run": True, "evidence_level": "normal"},
                    risk_level="low", confidence=1.0, source="dedup_run",
                    run_id=str(run_id), status="pending")
                db.add(review)
                db.commit()
            merge_one(db, review.id, keeper, loser, rule, stats, via="dedup-button")

        p.update({k: v for k, v in stats.items()})
        p["remaining"] = max(len(merge) - limit, 0)

        if include_review:
            staged = 0
            # Identical names the rule refused (two real domains, a conflicting
            # city...) are exactly what a person should look at; near names too.
            for keeper, other, why in held_all[:limit]:
                if not why.startswith("a person"):
                    staged += _stage_review(db, keeper, other, {"name_similarity": 1.0, "held_because": why}, 1.0)
            for a, b_, score in near[:limit]:
                staged += _stage_review(db, a, b_, {"name_similarity": round(score / 100, 3)}, score / 100)
            db.commit()
            p["staged_reviews"] = staged
        return p
    except Exception as exc:
        db.rollback()
        logger.error(f"[DedupRun] {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()
        _lock.release()
