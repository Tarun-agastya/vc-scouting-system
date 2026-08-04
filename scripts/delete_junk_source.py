"""
One-off cleanup: delete confirmed-junk startup records from a given source
domain, plus every DuplicateReview/SuppressedMatch row that references them,
plus their Qdrant points.

Built 4 Aug 2026 for the hochschule-biberach.de incident (see
DATA_INTEGRITY_PLAN.md's review-inbox-flooding writeup and
ingestion/web_scraper.py's SKIP_PATTERNS/_drop_numbered_sequences fix) — a
crawl wandered into off-topic university pages (professor recruitment,
governance, cafeteria, IT department, a partner-logo "our network" page) and
extracted their photo captions / established-firm names as ~124 fake
"startups", all with zero real evidence (no description, no website).

Safety: only ever deletes records where BOTH description and website are
empty — a record with any real evidence is left alone and reported instead
of deleted, so this can't accidentally remove a genuine startup that merely
shares a source domain with junk. Dry run by default; --apply to execute.
Mirrors scripts/dedup_sweep.py's dry-run/--apply convention.

Usage
-----
    python scripts/delete_junk_source.py --domain hochschule-biberach.de
    python scripts/delete_junk_source.py --domain hochschule-biberach.de --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch


def run(domain: str, apply: bool) -> None:
    db = SessionLocal()
    try:
        rows = db.query(Startup).filter(Startup.source_url.ilike(f"%{domain}%")).all()
        if not rows:
            print(f"No records found for domain '{domain}'.")
            return

        junk = [r for r in rows if not r.description and not r.website]
        keep = [r for r in rows if r not in junk]

        print(f"Domain: {domain}")
        print(f"Total records: {len(rows)}")
        print(f"Junk (no description, no website) — would delete: {len(junk)}")
        for r in junk:
            print(f"  DELETE  {r.id}  {r.name!r}  <- {r.source_url}")
        if keep:
            print(f"Has real evidence — would KEEP (not touched): {len(keep)}")
            for r in keep:
                print(f"  KEEP    {r.id}  {r.name!r}")

        if not apply:
            print("\nDry run only — nothing deleted. Re-run with --apply to execute.")
            return

        from vector_db.qdrant_store import qdrant_store

        ids = [str(r.id) for r in junk]
        reviews_deleted = db.query(DuplicateReview).filter(
            (DuplicateReview.master_id.in_(ids)) | (DuplicateReview.incoming_id.in_(ids))
        ).delete(synchronize_session=False)
        suppressions_deleted = db.query(SuppressedMatch).filter(
            (SuppressedMatch.master_id.in_(ids)) | (SuppressedMatch.other_id.in_(ids))
        ).delete(synchronize_session=False)

        for r in junk:
            db.delete(r)
        db.commit()

        qdrant_failures = 0
        for i in ids:
            try:
                qdrant_store.delete_startup(i)
            except Exception:
                qdrant_failures += 1

        print(f"\nDeleted {len(junk)} startup rows, {reviews_deleted} reviews, "
              f"{suppressions_deleted} suppressions.")
        print(f"Qdrant point deletes attempted: {len(ids)}, failures: {qdrant_failures}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, help="Source domain to clean up, e.g. hochschule-biberach.de")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default is dry run)")
    args = ap.parse_args()
    run(args.domain, args.apply)


if __name__ == "__main__":
    main()
