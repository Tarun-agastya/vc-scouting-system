"""
Repair the missing identity fingerprints that broke exact-match dedup.

The bug (found 22 Sep 2026)
---------------------------
A record created from a bare-name listing has no website, so it correctly
stores `fingerprint = NULL` and relies on the multi-signal matcher. When a
website was later filled in — by storage's auto-apply path or by approving a
review — nothing recomputed the fingerprint. It stayed NULL.

`matcher.build_match_report` tests exact identity with
`WHERE fingerprint = <computed>`. A NULL fingerprint can never match that, so
every one of those records became permanently invisible to exact-match dedup
and each re-crawl inserted a fresh copy.

Measured before this ran:

    3,591 records, of which
      2,215  no website        -> NULL fingerprint  (correct by design)
        693  website + fingerprint                  (healthy)
        683  website, NO fingerprint                <- the bug
      718 redundant records across 386 repeated names ("gameforge" x12,
          nine of them with a byte-identical website)
      668 "stable_id collision" warnings, accelerating every sweep:
          17 -> 20 -> 40 -> 51 -> 60 -> 86

The source-level fix is `storage.refresh_identity_fingerprint`, called from
both write paths. This script repairs the records already damaged.

What it does, and what it deliberately does not
-----------------------------------------------
`fingerprint` is UNIQUE, so a backfill cannot simply write every computed
value: where several rows are the same company, they all compute the SAME
fingerprint. Those collisions are not a problem to work around — they are the
duplicates themselves, finally made visible.

So:
  * **Singletons** — one record for a computed fingerprint. Set it. This is
    the whole repair for them, and it immediately makes them dedup-visible.
  * **Collision groups** — several records sharing a computed fingerprint.
    Set it on the OLDEST (the canonical original sighting, matching the
    matcher's own "oldest match wins" rule) and leave the rest untouched,
    reporting them as merge candidates.

It does **not** merge or delete anything. Merging 358 records is destructive,
irreversible without a backup, and a judgement call about real business data —
that stays a human decision. Run with no flags to see the report; the numbers
it prints are the input to that decision.

Usage
-----
    python3 scripts/backfill_fingerprints.py                 # dry run
    python3 scripts/backfill_fingerprints.py --apply
    python3 scripts/backfill_fingerprints.py --list-duplicates
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup
from processing.deduplicator import extract_domain, generate_fingerprint


def analyse(db):
    """Group every websited record by the fingerprint it *should* have."""
    groups = defaultdict(list)
    no_website = already_ok = 0

    for s in db.query(Startup).order_by(Startup.created_at.asc()).all():
        website = (s.website or "").strip()
        if not website or not extract_domain(website):
            no_website += 1
            continue
        fp = generate_fingerprint(s.name, website)
        if not fp:
            continue
        if s.fingerprint == fp:
            already_ok += 1
        groups[fp].append(s)

    return groups, no_website, already_ok


def run(apply: bool, list_duplicates: bool) -> None:
    db = SessionLocal()
    try:
        groups, no_website, already_ok = analyse(db)

        singles = {fp: g for fp, g in groups.items() if len(g) == 1}
        multi = {fp: g for fp, g in groups.items() if len(g) > 1}

        # `fingerprint` is UNIQUE, so never write a value some other row
        # already holds. Two ways that happens, both found the hard way:
        #   * inside a duplicate group, a NEWER copy may already carry the
        #     fingerprint while the oldest is the one missing it — writing the
        #     oldest then collides with its own sibling;
        #   * a stale value elsewhere could coincide.
        # Checking the live set covers both, and skipping is always safe: the
        # identity is already anchored on one row of that company, which is
        # all the exact-match path needs.
        taken = {
            fp for (fp,) in db.query(Startup.fingerprint)
            .filter(Startup.fingerprint.isnot(None)).all()
        }

        to_set_single, to_set_canonical, blocked, skipped_taken = [], [], [], []
        for fp, g in singles.items():
            row = g[0]
            if row.fingerprint:
                continue
            (to_set_single if fp not in taken else skipped_taken).append(row)

        for fp, g in multi.items():
            canonical = g[0]          # oldest, because analyse() sorted ascending
            if not canonical.fingerprint:
                if fp in taken:
                    # A sibling copy already anchors this identity.
                    skipped_taken.append(canonical)
                else:
                    to_set_canonical.append(canonical)
                    taken.add(fp)
            blocked.extend(g[1:])

        total = db.query(Startup).count()
        print(f"Records                                  : {total}")
        print(f"  no website -> NULL fingerprint (by design): {no_website}")
        print(f"  already correct                           : {already_ok}")
        print()
        print(f"Distinct computed fingerprints             : {len(groups)}")
        print(f"  singletons  -> safe to set               : {len(to_set_single)}")
        print(f"  collision groups (the real duplicates)   : {len(multi)}")
        print(f"    canonical (oldest) to set              : {len(to_set_canonical)}")
        print(f"    left untouched as merge candidates     : {len(blocked)}")
        print(f"  skipped, identity already anchored elsewhere: {len(skipped_taken)}")
        print()
        print(f"=> after this runs, {len(to_set_single) + len(to_set_canonical)} records become "
              f"visible to exact-match dedup")
        print(f"=> {len(blocked)} redundant records remain, for a human to decide on")

        if list_duplicates:
            print("\n--- collision groups (oldest first; the rest are the redundant copies) ---")
            for fp, g in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:40]:
                print(f"  {len(g)}x  {g[0].name[:44]:46} {(g[0].website or '')[:42]}")

        if not apply:
            print("\nDry run — nothing written. Re-run with --apply to set the fingerprints.")
            print("Nothing is ever merged or deleted by this script, with or without --apply.")
            return

        written = 0
        for row in to_set_single + to_set_canonical:
            website = (row.website or "").strip()
            row.fingerprint = generate_fingerprint(row.name, website)
            written += 1
        db.commit()
        print(f"\nWrote {written} fingerprints. No records merged or deleted.")
        print(f"{len(blocked)} redundant copies remain — see --list-duplicates.")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the fingerprints (default: dry run)")
    ap.add_argument("--list-duplicates", action="store_true",
                    help="show the collision groups, which are the real duplicates")
    args = ap.parse_args()
    run(args.apply, args.list_duplicates)


if __name__ == "__main__":
    main()
