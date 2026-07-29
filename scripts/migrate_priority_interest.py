"""
Phase Q migration (29 Jul): B2B/GmbH priority signal + manual interest marking.

Adds two columns to `startups`:
  is_gmbh          - deterministic (no LLM) name/description/address check,
                      backfilled by processing/reclassifier.py's existing
                      reclassify pass alongside business_model. NULL = not
                      yet checked (same backlog convention as classified_at).
  interest_status  - "interested" | "not_interested" | NULL (unset, the
                      default). Always a human action via the dashboard —
                      never auto-set. NULL on every existing row; nothing
                      to backfill.

business_model (Phase Q1) reuses the existing `business_model` column
(already on the model, never previously populated) and classified_at
(already exists) as its own resumability marker — no new column/backlog
tracking needed for it.

Safe to run multiple times (IF NOT EXISTS).

Usage:
    python scripts/migrate_priority_interest.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine


def run():
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS is_gmbh BOOLEAN"
        ))
        conn.commit()
        print("  ✓  startups.is_gmbh")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_startups_is_gmbh ON startups (is_gmbh)"
        ))
        conn.commit()
        print("  ✓  index on is_gmbh")

        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS interest_status VARCHAR(20)"
        ))
        conn.commit()
        print("  ✓  startups.interest_status")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_startups_interest_status ON startups (interest_status)"
        ))
        conn.commit()
        print("  ✓  index on interest_status")

        total = conn.execute(text("SELECT COUNT(*) FROM startups")).scalar()
        print(f"  ✓  {total} row(s) total — is_gmbh/business_model pending via the next reclassify pass, interest_status starts unset")


if __name__ == "__main__":
    print("Running Phase Q migration (is_gmbh + interest_status)...")
    run()
    print("Done.")
