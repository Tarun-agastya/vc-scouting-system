"""
Rule-based auto-merge of identical-name duplicates (A2 of the "more
autonomy" plan). No model involved.

Why a rule and not the LLM adjudicator
---------------------------------------
Measured 29 Sep: 261 of 316 pending duplicate reviews had IDENTICAL names —
"Chrono24 ~ Chrono24" — usually one record with a website and one without,
or the same domain on both. Nothing about those is a judgement call; they
are the same record seen twice, and they kept appearing because the
fingerprint lookup can't see them (see same_entity's use in
processing/matcher.py). A model adds latency and a chance of error to a
question a rule answers exactly.

What "safe" means here
-----------------------
Merging deletes a record. The safety net is not confidence, it is
recoverability: field_merge.merge_records writes a MergeSnapshot BEFORE
touching anything, so every merge here has a one-click Undo that restores
the deleted row under its original id. On top of that:

  * a rule, not a score — every condition below must hold, and a pair that
    fails any of them is left for a human, exactly as today;
  * a fresh, COMPLETE backup must exist (processing/backup.py) or nothing
    runs, because Undo covers a bad merge, not a bad disk;
  * capped per night, off by default, and every merge is listed on the
    dashboard card with its rule and an Undo button.

The rule (same_entity)
-----------------------
  1. identical normalized company name;
  2. never two DIFFERENT real domains. "Real" means not a multi-tenant host
     (linkedin.com) and not a listing site we found the company on
     (munich-startup.de) — clean_company_website strips those;
  3. no conflict on city, country, industry or founded_year: both sides
     populated and different is a veto. München/Munich are not a conflict
     (field_policy.norm_value folds them);
  4. positive evidence beyond the name: the same domain on BOTH sides, the
     same SOURCE PAGE, or a matching city / founded year. A domain on only
     one side does not count;
  5. neither record flagged by verification — unless the case rests on the
     shared source page, which is independent of the doubtful stored data.

BMW (bmwstartupgarage.com vs bmwgroup.com) fails rule 2 and stays human,
which is the point: two real, different domains under one name is the exact
shape of "same name, different company".
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Backups older than this don't count as "fresh". 26h, not 24h: the backup
# runs at 00:30 and the resolver at 01:00, so a healthy chain is ~0.5h old.
# The slack absorbs a late run without opening the gate to a genuinely
# stale one.
MAX_BACKUP_AGE_HOURS = 26

_CONFLICT_FIELDS = ("city", "country", "industry", "founded_year")


def _get(rec, attr):
    return rec.get(attr) if isinstance(rec, dict) else getattr(rec, attr, None)


def _source_urls(rec) -> set:
    """
    Every page this record was extracted from. A record's provenance is
    evidence about identity that its stored fields often can't give: a stub
    with no website and no city still knows which page it came from.
    """
    urls = set()
    hist = rec.get("source_history") if isinstance(rec, dict) else getattr(rec, "source_history", None)
    for h in (hist or []):
        if isinstance(h, dict) and h.get("url"):
            urls.add(h["url"])
    single = _get(rec, "source_url")
    if single:
        urls.add(single)
    return {u.strip().lower().rstrip("/") for u in urls if u}


def real_domain(website: Optional[str]) -> Optional[str]:
    """The company's own registrable domain, or None if it has none we trust."""
    from processing.matcher import _identity_domain
    from processing.storage import clean_company_website

    cleaned = clean_company_website(website or "", "")
    return _identity_domain(cleaned) if cleaned else None


def same_entity(a, b) -> tuple:
    """
    (True, "rule text") when a and b are safely the same company, else
    (False, "why not"). `a`/`b` are Startup rows or plain dicts — the ingest
    path passes the incoming extraction as a dict, the batch job passes rows.
    """
    from processing.deduplicator import normalize_company_name
    from processing.field_policy import norm_value

    na, nb = normalize_company_name(_get(a, "name") or ""), normalize_company_name(_get(b, "name") or "")
    if not na or na != nb:
        return False, "names differ"

    # Provenance: did both come from the same page? A listing does not list
    # two different companies under one identical name, so an identical name
    # from the SAME page is the same record seen twice — a re-crawl. Measured
    # 29 Sep: 179 of the 183 identical-name pairs the stricter rule had held
    # back shared a source page. This is also exactly why they regrow: each
    # re-crawl of that page re-creates the pair.
    shared_page = bool(_source_urls(a) & _source_urls(b))

    # A record verification has flagged is one whose stored data we trust
    # least, and identity decided FROM stored data (a domain, a city) would
    # inherit that doubt. Identity decided from provenance does not — merging
    # two copies of one page's extraction adds no doubt — so the veto applies
    # only when the case rests on stored fields.
    if not shared_page:
        for rec in (a, b):
            if (_get(rec, "verification_status") or "") == "flagged":
                return False, "a record is flagged by verification"

    da, db_ = real_domain(_get(a, "website")), real_domain(_get(b, "website"))
    if da and db_ and da != db_:
        return False, f"two real domains ({da} vs {db_})"

    for f in _CONFLICT_FIELDS:
        va, vb = _get(a, f), _get(b, f)
        if va in (None, "", 0) or vb in (None, "", 0):
            continue
        # industry is a coarse label a classifier assigns per copy; two copies
        # of one page's record differing on it is classifier noise, not two
        # companies. From a shared page it does not veto. city, country and
        # founded year are extracted facts, and still do.
        if f == "industry" and shared_page:
            continue
        if norm_value(va) != norm_value(vb):
            return False, f"conflicting {f} ({va} vs {vb})"

    # Positive evidence beyond the name. A shared name alone proves nothing
    # ("Bird", "Nova"). What counts:
    #   * the same domain on BOTH sides;
    #   * the same source page (provenance, above);
    #   * a matching city or founded year.
    # A domain on only ONE side is deliberately not evidence — it says nothing
    # about a record with no website — and industry/country are coarse, so
    # agreeing on them is close to free and never counts.
    if da and db_:
        return True, "identical name, same domain " + da
    if shared_page:
        return True, "identical name, same source page"
    agreeing = [f for f in ("city", "founded_year")
                if _get(a, f) not in (None, "", 0) and _get(b, f) not in (None, "", 0)
                and norm_value(_get(a, f)) == norm_value(_get(b, f))]
    if not agreeing:
        return False, "identical name but no shared domain, page, city or founded year"
    return True, "identical name, agree on " + "/".join(agreeing)


def _repoint_or_close_other_reviews(db, loser_id, keeper_id, this_review_id) -> dict:
    """
    merge_records deletes the loser but knows nothing about OTHER pending
    reviews that name it — DZ.S alone has three reviews for one pair. Left
    alone they would point at a record that no longer exists.

      same pair (the other side is the keeper)  -> moot, closed as approved
      pending review naming the loser elsewhere  -> repointed to the keeper
    Undo restores the loser row; repointed reviews stay on the keeper, which
    is where their proposals were always going to apply.
    """
    from database.models import DuplicateReview

    moot = repointed = 0
    rows = db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending",
        DuplicateReview.id != this_review_id,
        (DuplicateReview.master_id == loser_id) | (DuplicateReview.incoming_id == loser_id),
    ).all()
    for r in rows:
        other = r.incoming_id if r.master_id == loser_id else r.master_id
        if r.review_type in ("possible_duplicate", "anomaly") and other == keeper_id:
            r.status, r.resolved_at = "approved", datetime.utcnow()
            ev = dict(r.evidence or {})
            ev["auto_merge_moot"] = "same pair was merged by review " + str(this_review_id)
            r.evidence = ev
            moot += 1
            continue
        if r.master_id == loser_id:
            r.master_id = keeper_id
        if r.incoming_id == loser_id:
            r.incoming_id = keeper_id
        repointed += 1
    # What a person already decided about the merged-away record must survive
    # it: "X is a different company from <loser>" and "value V was rejected
    # for <loser>". Left pointing at a deleted id those decisions silently
    # stopped applying, so the pair was flagged again and the rejected value
    # came back. Repoint them at the keeper.
    from database.models import SuppressedMatch

    carried = 0
    for sm in db.query(SuppressedMatch).filter(
            (SuppressedMatch.master_id == loser_id) | (SuppressedMatch.other_id == loser_id)).all():
        if sm.master_id == loser_id:
            sm.master_id = keeper_id
        if sm.other_id == loser_id:
            sm.other_id = keeper_id
        if sm.kind == "known_different" and sm.master_id == sm.other_id:
            db.delete(sm)                  # the pair collapsed into itself: nothing left to remember
            continue
        carried += 1
    return {"moot": moot, "repointed": repointed, "suppressions_carried": carried}


def backup_gate() -> tuple:
    """(ok, why). Auto-merge without a restore path is not allowed."""
    from processing.backup import latest_backup_age_hours

    age = latest_backup_age_hours()
    if age is None:
        return False, "no complete backup exists"
    if age > MAX_BACKUP_AGE_HOURS:
        return False, f"latest backup is {age:.1f}h old (limit {MAX_BACKUP_AGE_HOURS}h)"
    return True, f"latest backup {age:.1f}h old"


def find_eligible(db, limit: int = 50, master_ids=None):
    """
    Scan pending possible_duplicate reviews. Returns (eligible, rejected):
    eligible = [(review, keeper, loser, rule)], rejected = Counter of reasons.
    Read-only.
    """
    from collections import Counter

    from database.models import DuplicateReview, Startup

    q = db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending",
        DuplicateReview.review_type == "possible_duplicate",
        DuplicateReview.incoming_id.isnot(None))
    if master_ids is not None:
        q = q.filter(DuplicateReview.master_id.in_(master_ids))

    eligible, rejected, seen_losers, seen_keepers = [], Counter(), set(), set()
    for r in q.order_by(DuplicateReview.created_at.asc()).all():
        keeper = db.query(Startup).filter(Startup.id == r.master_id).first()
        loser = db.query(Startup).filter(Startup.id == r.incoming_id).first()
        if keeper is None or loser is None or keeper.id == loser.id:
            rejected["a record no longer exists"] += 1
            continue
        if loser.id in seen_losers:          # already claimed by an earlier pair this scan
            continue
        # A record must never be BOTH a keeper and a loser within one batch.
        # Found on the first real run (30 Sep): three copies of "DZ.S" gave
        # pairs (A,B) and (B,C). B was merged into A and deleted, then B was
        # used as the KEEPER of the second pair — so C was merged into a
        # record that no longer existed. The snapshot saved it, but the
        # merge must never be attempted. The skipped pair is not lost: the
        # first merge repoints its review to the surviving record, and the
        # next run pairs them properly.
        if keeper.id in seen_losers or loser.id in seen_keepers:
            rejected["chained with another merge in this batch (next run)"] += 1
            continue
        ok, why = same_entity(keeper, loser)
        if not ok:
            rejected[why.split(" (")[0]] += 1
            continue
        eligible.append((r, keeper, loser, why))
        seen_losers.add(loser.id)
        seen_keepers.add(keeper.id)
        if len(eligible) >= limit:
            break
    return eligible, rejected


def merge_one(db, review_id, keeper, loser, rule: str, stats: dict, *, via: str = "nightly") -> bool:
    """
    Merge one pair, close its review, and tidy everything that pointed at the
    loser. The single place a rule-based merge happens — the nightly job and
    the Browse "Deduplicate" button both come through here, so what is checked,
    snapshotted and repointed can never differ between them.

    Returns True if merged. Never raises: a failure is rolled back, counted in
    stats["failed"], and logged. `via` is recorded on the review's audit entry
    so the Recent merges list can say who did it.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from database.models import DuplicateReview, Startup
    from processing.field_merge import build_merge_preview, merge_records

    try:
        loser_id, keeper_id = loser.id, keeper.id
        # Trust nothing read earlier: an earlier merge in this loop may have
        # consumed either record. Re-check at the moment of use.
        if (db.query(Startup).filter(Startup.id == keeper_id).first() is None
                or db.query(Startup).filter(Startup.id == loser_id).first() is None):
            stats["skipped_stale"] = stats.get("skipped_stale", 0) + 1
            return False
        # ...and a person may have settled this review meanwhile.
        review = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
        if review is None or review.status != "pending":
            stats["skipped_stale"] = stats.get("skipped_stale", 0) + 1
            return False

        choices = {f["field"]: f["default"] for f in build_merge_preview(keeper, loser)}
        result = merge_records(db, keeper, loser, choices, review_id=review_id)

        review = db.query(DuplicateReview).filter(DuplicateReview.id == review_id).first()
        review.status, review.resolved_at = "approved", datetime.utcnow()
        ev = dict(review.evidence or {})
        ev["auto_merge"] = {"rule": rule, "snapshot_id": result["snapshot_id"], "via": via,
                            "at": datetime.utcnow().isoformat(timespec="seconds")}
        review.evidence = ev
        flag_modified(review, "evidence")
        stats.update({k: stats.get(k, 0) + v for k, v in
                      _repoint_or_close_other_reviews(db, loser_id, keeper_id, review_id).items()})
        db.commit()

        from api.routes.reviews import _reindex
        _reindex(db, db.query(Startup).filter(Startup.id == keeper_id).first())
        stats["merged"] = stats.get("merged", 0) + 1
        return True
    except Exception as exc:
        db.rollback()
        stats["failed"] = stats.get("failed", 0) + 1
        logger.error(f"[AutoMerge] {getattr(loser, 'name', '?')}: {type(exc).__name__}: {exc}")
        return False


def auto_merge_pending(limit: int = 50, *, apply: bool = False, master_ids=None,
                       require_backup: bool = True) -> dict:
    """
    Merge eligible identical-name duplicates. Dry-run unless apply=True.

    Never raises; returns a stats dict. `require_backup=False` exists only so
    tests can run without touching real backups — every production caller
    leaves it True.
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from processing.field_merge import build_merge_preview, merge_records

    stats = {"eligible": 0, "merged": 0, "failed": 0}

    # require_backup=False exists only so tests can run without touching real
    # backups. Without master_ids it would merge REAL records with no restore
    # point — and a test that demonstrated exactly that did so on 30 Sep,
    # merging 5 live pairs (recoverable from their snapshots, but a mistake).
    # So the escape hatch is refused unless the run is scoped to named records.
    if apply and not require_backup and master_ids is None:
        stats["blocked"] = "require_backup=False is only allowed together with master_ids"
        return stats

    db = SessionLocal()
    try:
        if apply and require_backup:
            ok, why = backup_gate()
            if not ok:
                stats["blocked"] = why
                logger.warning(f"[AutoMerge] refusing to run: {why}")
                return stats

        eligible, rejected = find_eligible(db, limit, master_ids)
        stats["eligible"] = len(eligible)
        stats.update({f"rejected: {k}": v for k, v in rejected.items()})
        stats["_pairs"] = [(k.name, l.name, rule) for _r, k, l, rule in eligible]
        if not apply:
            return stats

        for review, keeper, loser, rule in eligible:
            merge_one(db, review.id, keeper, loser, rule, stats)
    except Exception as exc:
        db.rollback()
        stats["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        db.close()
    return stats
