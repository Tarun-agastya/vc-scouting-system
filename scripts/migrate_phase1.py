"""
Phase 1 database migration.

Adds the new columns introduced in the architecture redesign to an
existing `startups` table.  Safe to run multiple times — every statement
uses IF NOT EXISTS or PL/pgSQL guards.

Usage:
    python scripts/migrate_phase1.py
"""
import sys
import os

# Allow running from project root or scripts/ directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine

# Each entry is (description, SQL).
# PostgreSQL's "ALTER TABLE … ADD COLUMN IF NOT EXISTS" (v9.6+) is used
# throughout so re-runs are completely safe.
MIGRATIONS = [
    (
        "source_history JSON column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS source_history JSON DEFAULT '[]'::json",
    ),
    (
        "normalized_name column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS normalized_name VARCHAR(255)",
    ),
    (
        "fingerprint column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(64)",
    ),
    (
        "contact_info column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS contact_info VARCHAR(500)",
    ),
    (
        "linkedin column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS linkedin VARCHAR(500)",
    ),
    (
        "enrichment_score column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS enrichment_score FLOAT DEFAULT 0.0",
    ),
    (
        "last_enriched_at column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS last_enriched_at TIMESTAMP",
    ),
    (
        "published_at column (idempotent — may already exist)",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
    ),
    # Unique constraint on fingerprint: use PL/pgSQL guard so it's safe to re-run
    (
        "UNIQUE constraint on fingerprint",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_startups_fingerprint'
                  AND conrelid = 'startups'::regclass
            ) THEN
                ALTER TABLE startups
                    ADD CONSTRAINT uq_startups_fingerprint UNIQUE (fingerprint);
            END IF;
        END $$
        """,
    ),
    (
        "index on normalized_name",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'startups'
                  AND indexname = 'ix_startups_normalized_name'
            ) THEN
                CREATE INDEX ix_startups_normalized_name
                    ON startups (normalized_name);
            END IF;
        END $$
        """,
    ),
    (
        "index on fingerprint",
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'startups'
                  AND indexname = 'ix_startups_fingerprint'
            ) THEN
                CREATE INDEX ix_startups_fingerprint ON startups (fingerprint);
            END IF;
        END $$
        """,
    ),
]


def run() -> None:
    print("── Phase 1 Migration ──────────────────────────────────────────")
    ok = 0
    failed = 0
    with engine.connect() as conn:
        for description, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"  ✓  {description}")
                ok += 1
            except Exception as exc:
                conn.rollback()
                print(f"  ✗  {description}: {exc}")
                failed += 1

    print(f"\n  {ok} applied, {failed} failed.")
    if failed:
        print("  Review errors above — some columns/constraints may need manual attention.")
    else:
        print("  Migration complete. Safe to restart the API server.")


if __name__ == "__main__":
    run()
