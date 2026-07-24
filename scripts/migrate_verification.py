"""
Phase H-2 migration: verification/recheck status model.

Adds five columns to `startups`:
  verification_status    - unverified | verified | flagged (default 'unverified')
  verification_notes     - human/LLM-readable recheck summary
  verification_evidence  - JSON per-field grounded/nulled/flagged detail
  verified_at             - when the last recheck ran
  source_excerpt          - the extraction chunk this record came from (Phase H-1)

Backfills every existing row to verification_status='unverified' — owner
decision (plan addendum 3): recheck existing + new, not just new ingests
going forward, so the first Phase H-3 "Recheck now" run re-grounds the
whole DB, not only records ingested after H-1 shipped.

Safe to run multiple times (IF NOT EXISTS / idempotent backfill).

Usage:
    python scripts/migrate_verification.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine


def run():
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS "
            "verification_status VARCHAR(20) DEFAULT 'unverified'"
        ))
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS verification_notes TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS verification_evidence JSON"
        ))
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE startups ADD COLUMN IF NOT EXISTS source_excerpt TEXT"
        ))
        conn.commit()
        print("  ✓  startups.verification_status / verification_notes / "
              "verification_evidence / verified_at / source_excerpt")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_startups_verification_status "
            "ON startups (verification_status)"
        ))
        conn.commit()
        print("  ✓  index on verification_status")

        # Backfill: every existing row starts unverified. ADD COLUMN ... DEFAULT
        # already populates existing rows in Postgres, but this UPDATE makes the
        # intent explicit and is a harmless no-op if it already ran.
        result = conn.execute(text(
            "UPDATE startups SET verification_status = 'unverified' "
            "WHERE verification_status IS NULL"
        ))
        conn.commit()
        print(f"  ✓  backfilled {result.rowcount} existing row(s) → 'unverified'")

        total = conn.execute(text(
            "SELECT COUNT(*) FROM startups WHERE verification_status = 'unverified'"
        )).scalar()
        print(f"  ✓  {total} row(s) now marked 'unverified' total")


if __name__ == "__main__":
    print("Running Phase H-2 verification-status migration...")
    run()
    print("Done.")
