"""
Phase S-3b migration: data-stewardship review model + extraction timestamp.

- Adds `startups.extracted_at` (when the pipeline captured the record; date+time).
- Recreates the `duplicate_reviews` table with the unified review schema
  (review_type / master_* / incoming_* / proposed_changes / evidence /
  risk_level / llm_explanation / …). The table was only ever populated by
  the matcher and was empty the FIRST time this ran, so a drop+recreate was
  safe then.
- Creates the new `suppressed_matches` table (reject-suppression guardrail).

Idempotent by design (13 Aug 2026 fix): originally this dropped
duplicate_reviews UNCONDITIONALLY on every run despite the module and this
docstring both claiming "safe to run multiple times" — true only at the
moment this migration was first written. By the time review_type (a new-
schema column) exists, the table holds real, human-curated review state
(pending/approved/rejected rows the whole Review Inbox depends on); a
second run — from confusion, a fresh-environment bootstrap loop over
scripts/migrate_*.py, or just this script looking exactly like every other
idempotent one in this directory — would silently destroy all of it with no
confirmation and no backup. Now: if the new schema already exists, this is a
no-op; if the table has any rows at all (old OR new schema — never assume
which), it refuses and asks for --force rather than guessing.

Usage:
    python scripts/migrate_reviews.py
    python scripts/migrate_reviews.py --force   # only if you KNOW the table
                                                  # is safe to drop and rows
                                                  # already there are meant
                                                  # to be discarded
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text, inspect as sa_inspect
from database.connection import engine
from database.models import Base, DuplicateReview, SuppressedMatch


def run(force: bool = False):
    inspector = sa_inspect(engine)

    with engine.connect() as conn:
        # 1. extraction timestamp on startups
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMP"
        ))
        conn.commit()
        print("  ✓  startups.extracted_at")

        # 2. drop the old review table -- but only if it's actually the OLD
        # schema and actually empty. Never drop blind.
        if "duplicate_reviews" in inspector.get_table_names():
            columns = {c["name"] for c in inspector.get_columns("duplicate_reviews")}
            already_migrated = "review_type" in columns
            row_count = conn.execute(text("SELECT COUNT(*) FROM duplicate_reviews")).scalar()

            if already_migrated and not force:
                print("  ·  duplicate_reviews already has the unified schema — nothing to do")
                return
            if row_count and not force:
                print(
                    f"  ✗  duplicate_reviews has {row_count} row(s) — refusing to drop. "
                    "This migration was only ever safe to run once, against an empty "
                    "table. Re-run with --force if you are certain these rows should "
                    "be discarded."
                )
                sys.exit(1)

            conn.execute(text("DROP TABLE IF EXISTS duplicate_reviews"))
            conn.commit()
            print("  ✓  dropped old duplicate_reviews")
        else:
            print("  ·  duplicate_reviews does not exist yet — nothing to drop")

    # 3. recreate duplicate_reviews (new schema) + create suppressed_matches
    Base.metadata.create_all(
        bind=engine,
        tables=[DuplicateReview.__table__, SuppressedMatch.__table__],
    )
    print("  ✓  created duplicate_reviews (unified schema)")
    print("  ✓  created suppressed_matches")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Drop duplicate_reviews even if it already has rows or the new schema")
    args = parser.parse_args()

    print("Running Phase S-3b review-model migration...")
    run(force=args.force)
    print("Done.")
