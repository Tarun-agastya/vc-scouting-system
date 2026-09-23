"""
Creates `merge_snapshots` — the table that makes a merge reversible.

Written for Phase 3 of the CRM plan (field-level merge). The snapshot is taken
before any merge touches a record, because the old fill-blanks merge deleted
the losing row and its Qdrant point with no way back. Field-level merge
removes that merge's accidental safety — it could only fill EMPTY fields, so
it could never destroy anything, and replacing a populated value is the entire
point of the new feature.

See database/models.py::MergeSnapshot for the column reference.

Starts empty. Safe to run repeatedly (create_all's checkfirst), same as
scripts/migrate_site_profiles.py.

Usage:
    python3 scripts/migrate_merge_snapshots.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import engine
from database.models import Base, MergeSnapshot


def run():
    Base.metadata.create_all(bind=engine, tables=[MergeSnapshot.__table__])
    print("  ✓  created merge_snapshots (or already existed)")


if __name__ == "__main__":
    run()
