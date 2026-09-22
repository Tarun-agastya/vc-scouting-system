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


def _source_site_rows(db) -> list:
    """Rows whose `website` is really the listing site they were found on."""
    from processing.storage import clean_company_website
    out = []
    for s in db.query(Startup).all():
        w = (s.website or "").strip()
        if w and clean_company_website(w, s.name) == "":
            out.append(s)
    return out


def _tier_c(db, ignore_ids=None):
    """
    Same normalized name, and at most ONE distinct real domain across the group.

    `ignore_ids` are rows whose website is about to be cleared, so the dry run
    predicts what --apply will actually do rather than the pre-cleaning state.
    Without it the preview under-reported, which defeats the point of a preview.

    Safe because nothing in the group contradicts anything else: either no row
    claims a domain, or exactly one does and the rest are silent. Groups where
    two rows claim DIFFERENT domains are excluded — that is a genuine identity
    question (a rebrand? two companies?) and belongs to a human or the
    adjudicator, not to a bulk script.
    """
    from processing.deduplicator import extract_domain
    ignore_ids = ignore_ids or set()
    buckets = defaultdict(list)
    for s in db.query(Startup).order_by(Startup.created_at.asc()).all():
        key = (s.normalized_name or s.name or "").strip().lower()
        if key:
            buckets[key].append(s)

    safe, conflicted = {}, {}
    for key, g in buckets.items():
        if len(g) < 2:
            continue
        domains = {
            "" if x.id in ignore_ids else extract_domain(x.website or "")
            for x in g
        }
        domains.discard("")
        (safe if len(domains) <= 1 else conflicted)[key] = g
    return safe, conflicted


def _cleanup_refs(db, loser_id) -> tuple:
    """Delete reviews/suppressions pointing at a row that is about to vanish."""
    r = db.query(DuplicateReview).filter(
        (DuplicateReview.master_id == loser_id) | (DuplicateReview.incoming_id == loser_id)
    ).delete(synchronize_session=False)
    s = db.query(SuppressedMatch).filter(
        (SuppressedMatch.master_id == loser_id) | (SuppressedMatch.other_id == loser_id)
    ).delete(synchronize_session=False)
    return r, s


def _merge_group(db, g) -> tuple:
    """Merge every row in `g` into the oldest. Returns (merged, reviews, supps)."""
    from api.routes.reviews import _merge_records
    from processing.storage import refresh_identity_fingerprint

    keeper, losers = g[0], g[1:]
    merged = reviews = supps = 0
    for loser in losers:
        r, sp = _cleanup_refs(db, loser.id)
        reviews += r
        supps += sp
        _merge_records(db, keeper, loser, loser.raw_data or {})
        merged += 1
    refresh_identity_fingerprint(keeper)
    db.commit()
    return merged, reviews, supps


def run(apply: bool, limit: int, report_tier_b: bool,
        clean_source_websites: bool = False, tier_c: bool = False) -> None:
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

        if clean_source_websites:
            bad = _source_site_rows(db)
            print(f"\nSource-site websites to clear: {len(bad)}")
            for s_ in bad[:6]:
                print(f"    {s_.name[:34]:36} {(s_.website or '')[:44]}")

        if tier_c:
            pending_clear = {r.id for r in _source_site_rows(db)} if clean_source_websites else set()
            safe, conflicted = _tier_c(db, ignore_ids=pending_clear)
            print(f"\nTier C — same name, at most one distinct domain (safe to merge)")
            print(f"  groups: {len(safe)}   rows merged away: {sum(len(g)-1 for g in safe.values())}")
            print(f"\nExcluded — same name but CONFLICTING domains (left for a human)")
            print(f"  groups: {len(conflicted)}   rows: {sum(len(g)-1 for g in conflicted.values())}")
            for k, g in sorted(conflicted.items(), key=lambda kv: -len(kv[1]))[:8]:
                from processing.deduplicator import extract_domain
                doms = [extract_domain(x.website or "") or "(none)" for x in g]
                print(f"    {len(g)}x  {k[:30]:32} {doms[:4]}")

        if not apply:
            print("\nDry run — nothing merged or deleted. Re-run with --apply.")
            print("Take a backup first; this cannot be undone.")
            return

        if clean_source_websites:
            from processing.storage import refresh_identity_fingerprint
            cleared = 0
            for s_ in _source_site_rows(db):
                s_.website = None
                refresh_identity_fingerprint(s_)
                cleared += 1
            db.commit()
            print(f"\nCleared {cleared} source-site websites (web-verify can refill them properly).")

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

        if tier_c:
            safe, _ = _tier_c(db)
            for g in sorted(safe.values(), key=lambda g: -len(g)):
                m, r, sp = _merge_group(db, g)
                merged += m
                reviews_gone += r
                supps_gone += sp

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
    ap.add_argument("--clean-source-websites", action="store_true",
                    help="first clear websites that are really the listing site")
    ap.add_argument("--tier-c", action="store_true",
                    help="also merge same-name groups with at most one distinct domain")
    args = ap.parse_args()
    run(args.apply, args.limit, args.report_tier_b,
        args.clean_source_websites, args.tier_c)


if __name__ == "__main__":
    main()
