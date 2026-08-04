"""
One-off cleanup: delete existing startup records whose NAME matches
reasoning.qwen_client._is_implausible_startup_name (Phase J) — article
headlines, multi-entity comma lists, institutions/incumbents, and
fabricated generic-noun-phrase fragments extracted before this gate
existed. This runs the gate retroactively against already-stored data,
not just new extractions.

Distinct from scripts/delete_junk_source.py's blanket "no description AND
no website" filter: a source can carry a MIX of real bare-stub startups
and junk (confirmed 4 Aug on munich-startup.de — ElevenLabs, Mistral AI,
Stocard etc. sit alongside headline/person-name junk, all similarly
evidence-free). The blanket filter would have deleted the real companies
too. This script only deletes a NAME-level match — a much narrower,
higher-confidence signal that a real bare-stub startup will never trip
(verified 4 Aug against the whole DB with zero false positives before
this gate shipped).

Also deletes every DuplicateReview/SuppressedMatch referencing a matched
record, plus its Qdrant point. Dry run by default; --apply to execute.

Usage
-----
    python scripts/delete_implausible_names.py                              # whole DB, dry run
    python scripts/delete_implausible_names.py --domain munich-startup.de   # scoped, dry run
    python scripts/delete_implausible_names.py --domain munich-startup.de --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch
from reasoning.qwen_client import _is_implausible_startup_name
from config.tuning_loader import get_institutional_junk_config


def run(domain: str, apply: bool) -> None:
    db = SessionLocal()
    try:
        query = db.query(Startup)
        if domain:
            query = query.filter(Startup.source_url.ilike(f"%{domain}%"))
        rows = query.all()
        if not rows:
            print(f"No records found{f' for domain {domain!r}' if domain else ''}.")
            return

        cfg = get_institutional_junk_config()
        junk = [r for r in rows if _is_implausible_startup_name(r.name, cfg)]

        print(f"Scope: {domain or 'whole database'}")
        print(f"Total records scanned: {len(rows)}")
        print(f"Matched _is_implausible_startup_name — would delete: {len(junk)}")
        for r in junk:
            print(f"  DELETE  {r.id}  {r.name!r}  <- {r.source_url}")

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
    ap.add_argument("--domain", default=None, help="Restrict to a source domain, e.g. munich-startup.de (default: whole DB)")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default is dry run)")
    args = ap.parse_args()
    run(args.domain, args.apply)


if __name__ == "__main__":
    main()
