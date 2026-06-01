"""
Phase 3 database migration.

Adds the scoring columns introduced by processing/scorer.py to an existing
`startups` table.  Safe to run multiple times — every statement uses
IF NOT EXISTS or PL/pgSQL guards matching the migrate_phase1.py convention.

New columns
-----------
  source_confidence FLOAT        — 0-100 extraction trust score (separate from enrichment_score)
  score_breakdown   JSON         — full explainable breakdown (see processing/scorer.py)
  score_tier        VARCHAR(50)  — cached tier label: WEAK_SIGNAL / EARLY_DISCOVERY /
                                   INTERESTING / HIGH_QUALITY_LEAD / PRIORITY

New index
---------
  ix_startups_score_tier  — enables fast WHERE score_tier = 'PRIORITY' queries

Usage
-----
    python scripts/migrate_phase3.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine

MIGRATIONS = [
    (
        "source_confidence column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS source_confidence FLOAT DEFAULT 0.0",
    ),
    (
        "score_breakdown column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS score_breakdown JSON",
    ),
    (
        "score_tier column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS score_tier VARCHAR(50)",
    ),
    (
        "index on score_tier",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'startups'
                  AND indexname = 'ix_startups_score_tier'
            ) THEN
                CREATE INDEX ix_startups_score_tier ON startups (score_tier);
            END IF;
        END $$
        """,
    ),
]


def run_migrations():
    with engine.connect() as conn:
        for description, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"  ✓  {description}")
            except Exception as exc:
                conn.rollback()
                print(f"  ✗  {description}: {exc}")
                raise


if __name__ == "__main__":
    print("Running Phase 3 migrations...")
    run_migrations()
    print("Done.")
