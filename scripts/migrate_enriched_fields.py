"""
Database migration: enriched startup profile fields.

Adds the new extraction fields requested for richer startup profiles:
  - address        VARCHAR(500)  — street address or city+country
  - tech_cluster   VARCHAR(200)  — specific technology domain/cluster

short_description and employee_count already exist in the schema.

Safe to run multiple times — all statements use IF NOT EXISTS guards.

Usage
-----
    python scripts/migrate_enriched_fields.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from database.connection import engine

MIGRATIONS = [
    (
        "address column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS address VARCHAR(500)",
    ),
    (
        "tech_cluster column",
        "ALTER TABLE startups ADD COLUMN IF NOT EXISTS tech_cluster VARCHAR(200)",
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
    print("Running enriched profile field migrations...")
    run_migrations()
    print("Done.")
