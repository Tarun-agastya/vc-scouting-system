"""
Creates `field_changes` — the per-record change history (Phase 2 of the CRM plan).

`source_history` already recorded where a record was SEEN. This records what
CHANGED about it, which is the first thing anyone wants when a value looks
wrong and the panel can only show the value.

Starts empty and fills from the next write onward; there is no backfill,
because the information to backfill it with was never kept.

Safe to run repeatedly (create_all checkfirst).

Usage:
    python3 scripts/migrate_field_changes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import engine
from database.models import Base, FieldChange


def run():
    Base.metadata.create_all(bind=engine, tables=[FieldChange.__table__])
    print("  ✓  created field_changes (or already existed)")


if __name__ == "__main__":
    run()
