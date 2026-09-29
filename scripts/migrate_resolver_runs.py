"""
Creates `resolver_runs` — the durable record of what the nightly review
resolver/researcher did, surviving an API restart (Phase 4, plans/
REVIEW_INBOX_AUTONOMY_PLAN.md).

See database/models.py::ResolverRun for the column reference and why this
table exists — in short, ScoutController's run history is in-memory only,
which is fine for idempotent ingestion but not for a job that closes review
rows unattended.

Starts empty. Safe to run repeatedly (create_all's checkfirst), same as
scripts/migrate_merge_snapshots.py and scripts/migrate_field_changes.py.

Usage:
    python3 scripts/migrate_resolver_runs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import engine
from database.models import Base, ResolverRun


def run():
    Base.metadata.create_all(bind=engine, tables=[ResolverRun.__table__])
    print("  ✓  created resolver_runs (or already existed)")


if __name__ == "__main__":
    run()
