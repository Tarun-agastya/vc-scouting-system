"""
Phase S-3b migration: data-stewardship review model + extraction timestamp.

- Adds `startups.extracted_at` (when the pipeline captured the record; date+time).
- Recreates the `duplicate_reviews` table with the unified review schema
  (review_type / master_* / incoming_* / proposed_changes / evidence /
  risk_level / llm_explanation / …). The table is only ever populated by the
  matcher and was empty at migration time, so a drop+recreate is safe and
  cleaner than a long column-by-column ALTER.
- Creates the new `suppressed_matches` table (reject-suppression guardrail).

Safe to run multiple times.

Usage:
    python scripts/migrate_reviews.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine
from database.models import Base, DuplicateReview, SuppressedMatch


def run():
    with engine.connect() as conn:
        # 1. extraction timestamp on startups
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMP"
        ))
        conn.commit()
        print("  ✓  startups.extracted_at")

        # 2. drop the old review table (empty — populated only by the matcher)
        conn.execute(text("DROP TABLE IF EXISTS duplicate_reviews"))
        conn.commit()
        print("  ✓  dropped old duplicate_reviews")

    # 3. recreate duplicate_reviews (new schema) + create suppressed_matches
    Base.metadata.create_all(
        bind=engine,
        tables=[DuplicateReview.__table__, SuppressedMatch.__table__],
    )
    print("  ✓  created duplicate_reviews (unified schema)")
    print("  ✓  created suppressed_matches")


if __name__ == "__main__":
    print("Running Phase S-3b review-model migration...")
    run()
    print("Done.")
