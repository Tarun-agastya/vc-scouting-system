"""
Phase V-2 migration: controlled-taxonomy classification tracking.

Adds one column to `startups`:
  classified_at - when this record's industry/tech_cluster were last set to a
                  controlled config/taxonomy.yaml value (NULL = not yet, i.e.
                  still whatever free-form value extraction produced before
                  Phase V-2 shipped).

Deliberately does NOT touch existing industry/tech_cluster values or backfill
classified_at — leaving it NULL on every existing row is exactly what marks
the whole current backlog (~660 records) as eligible for
processing/reclassifier.py's one-time pass, per the owner's "reclassify all
now" decision (plan addendum 7 / Phase V-2).

Safe to run multiple times (IF NOT EXISTS).

Usage:
    python scripts/migrate_classification.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine


def run():
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS classified_at TIMESTAMP"
        ))
        conn.commit()
        print("  ✓  startups.classified_at")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_startups_classified_at "
            "ON startups (classified_at)"
        ))
        conn.commit()
        print("  ✓  index on classified_at")

        total = conn.execute(text("SELECT COUNT(*) FROM startups")).scalar()
        pending = conn.execute(text(
            "SELECT COUNT(*) FROM startups WHERE classified_at IS NULL"
        )).scalar()
        print(f"  ✓  {pending} of {total} row(s) pending controlled-taxonomy classification")


if __name__ == "__main__":
    print("Running Phase V-2 classification migration...")
    run()
    print("Done.")
