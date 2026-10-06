"""
Adds suppressed_matches.expires_at — machine-created suppressions lapse, a
person's never do. See database/models.py::SuppressedMatch. NULL (every
existing row) means permanent, so this changes no current behaviour.

Idempotent.   python3 scripts/migrate_suppression_expiry.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from database.connection import engine

if __name__ == "__main__":
    with engine.begin() as c:
        c.execute(text("ALTER TABLE suppressed_matches ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_suppressed_matches_expires_at "
                       "ON suppressed_matches (expires_at)"))
    print("  ✓  suppressed_matches.expires_at present")
