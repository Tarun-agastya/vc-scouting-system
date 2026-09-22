"""
Merge the duplicate records exposed by the fingerprint repair. Dry run by default.

Background
----------
`scripts/backfill_fingerprints.py` restored the identity fingerprints that
`storage.refresh_identity_fingerprint` had never been recomputing. Where several
rows are the same company they compute the SAME fingerprint, and the UNIQUE
constraint means only the oldest could take it. Those collision groups are the
duplicates, finally visible. This merges them.

Two tiers, and only one of them runs by default
-----------------------------------------------
**Tier A — same normalized name AND same registrable domain.** Very high
confidence: "gameforge" twelve times, nine of them pointing at
`www.gameforge.com`. These are the same company by any reading, and they are
what `--apply` merges.

**Tier B — same normalized name, neither has a website.** Reported, never
merged here. Two genuinely different companies can share a name with nothing to
tell them apart, which is precisely the risk `matcher.build_match_report`
documents as accepted-but-disclosed for its no-website path. Deciding those
needs either evidence this script doesn't have or a human. `--report-tier-b`
lists them.

Merging is not deletion
-----------------------
The newer copies frequently hold fields the oldest lacks — a description from
a later crawl, a funding stage, extra sources. Deleting them would throw that
away. So each loser is merged into the keeper through the same
`_merge_records` path the Review Inbox's own approve button uses: fill the
keeper's blank fields, union the source history, drop the Qdrant point, then
delete the row.

It also cleans up what `_merge_records` does not. Reviews and suppressions
pointing at a deleted record become orphans — that is how 1,348 orphaned
SuppressedMatch rows accumulated, and orphans are not harmless here, because
ids are deterministic: a company rediscovered later gets the same id back and
silently inherits a suppression from its previous life.

Safety
------
* Dry run by default; `--apply` required.
* Take a database backup first. This is irreversible:
      docker exec vc_postgres pg_dump -U scout -d vc_scouting --data-only \\
        -t startups -t duplicate_reviews -t suppressed_matches > backup.sql
* Keeper is always the OLDEST row, matching the matcher's own "the original
  sighting is canonical" rule.
* `--limit` caps how many groups are touched, so a first run can be small.

Usage
-----
    python3 scripts/merge_fingerprint_duplicates.py
    python3 scripts/merge_fingerprint_duplicates.py --report-tier-b
    python3 scripts/merge_fingerprint_duplicates.py --apply --limit 10
    python3 scripts/merge_fingerprint_duplicates.py --apply
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from processing.deduplicator import extract_domain, generate_fingerprint


def _groups(db):
    """Tier A groups: 2+ websited rows sharing a computed fingerprint."""
    buckets = defaultdict(list)
    for s in db.query(Startup).order_by(Startup.created_at.asc()).all():
        website = (s.website or "").strip()
        if not website or not extract_domain(website):
            continue
        fp = generate_fingerprint(s.name, website)
        if fp:
            buckets[fp].append(s)
    return {fp: g for fp, g in buckets.items() if len(g) > 1}


def _tier_b(db):
    """Same normalized name, no website on either side. Reported only."""
    buckets = defaultdict(list)
    for s in db.query(Startup).order_by(Startup.created_at.asc()).all():
        if (s.website or "").strip():
            continue
        key = (s.normalized_name or s.name or "").strip().lower()
        if key:
            buckets[key].append(s)
    return {k: g for k, g in buckets.items() if len(g) > 1}


def _cleanup_refs(db, loser_id) -> tuple:
    """Delete reviews/suppressions pointing at a row that is about to vanish."""
    r = db.query(DuplicateReview).filter(
        (DuplicateReview.master_id == loser_id) | (DuplicateReview.incoming_id == loser_id)
    ).delete(synchronize_session=False)
    s = db.query(SuppressedMatch).filter(
        (SuppressedMatch.master_id == loser_id) | (SuppressedMatch.other_id == loser_id)
    ).delete(synchronize_session=False)
    return r, s


def run(apply: bool, limit: int, report_tier_b: bool) -> None:
    db = SessionLocal()
    try:
        groups = _groups(db)
        total_losers = sum(len(g) - 1 for g in groups.values())
        print(f"Tier A — same name AND same domain")
        print(f"  duplicate groups        : {len(groups)}")
        print(f"  records to merge away   : {total_losers}")
        print(f"  records after merge     : {db.query(Startup).count() - total_losers}")

        print("\n  largest groups:")
        for fp, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]:
            k = g[0]
            print(f"    {len(g):>3}x  {k.name[:38]:40} keep {str(k.id)[:8]} "
                  f"({str(k.created_at)[:10]})  {(k.website or '')[:34]}")

        if report_tier_b:
            tb = _tier_b(db)
            print(f"\nTier B — same name, NO website on either side (reported, never merged)")
            print(f"  groups: {len(tb)}   redundant rows: {sum(len(g)-1 for g in tb.values())}")
            for k, g in sorted(tb.items(), key=lambda kv: -len(kv[1]))[:12]:
                print(f"    {len(g):>3}x  {k[:56]}")
            print("  These need evidence this script doesn't have — two different companies")
            print("  can share a name with nothing to separate them. Left alone deliberately.")

        if not apply:
            print("\nDry run — nothing merged or deleted. Re-run with --apply.")
            print("Take a backup first; this cannot be undone.")
            return

        merged = reviews_gone = supps_gone = 0
        from api.routes.reviews import _merge_records
        from processing.storage import refresh_identity_fingerprint

        for i, (fp, g) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
            if limit and i >= limit:
                print(f"\n--limit {limit} reached; stopping.")
                break
            keeper, losers = g[0], g[1:]
            for loser in losers:
                r, s = _cleanup_refs(db, loser.id)
                reviews_gone += r
                supps_gone += s
                # Same path the Review Inbox's approve button uses: fills the
                # keeper's blanks from the loser before removing it.
                _merge_records(db, keeper, loser, loser.raw_data or {})
                merged += 1
            refresh_identity_fingerprint(keeper)
            db.commit()

        print(f"\nMerged away {merged} duplicate records.")
        print(f"Removed {reviews_gone} now-moot reviews and {supps_gone} orphaned suppressions.")
        print(f"Records remaining: {db.query(Startup).count()}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually merge (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="cap groups touched this run (0 = all)")
    ap.add_argument("--report-tier-b", action="store_true",
                    help="also list same-name/no-website groups, which are NOT merged")
    args = ap.parse_args()
    run(args.apply, args.limit, args.report_tier_b)


if __name__ == "__main__":
    main()
