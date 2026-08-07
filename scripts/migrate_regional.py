"""
Phase RC-1 migration (6 Aug 2026) — regional company register.

Creates the `regional_companies` table: established SMEs (100-4000 employees)
within 50 km of Memmingen, tracked as GreenTech Hub membership prospects.
See database/models.py::RegionalCompany for the column reference and
REGIONAL_SME_PLAN.md for why this is a separate table from `startups`.

Touches nothing else. The startups/reviews/site_profiles tables are not read,
altered or dropped — this only ever adds one new table.

The table starts empty; populate it with:
    python3 scripts/rc_import_sheet.py --csv <exported sheet>.csv

Safe to run multiple times (create_all's checkfirst, same as
scripts/migrate_site_profiles.py).

Usage:
    python3 scripts/migrate_regional.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from database.connection import engine  # noqa: E402
from database.models import Base, RegionalCompany  # noqa: E402


def run():
    Base.metadata.create_all(bind=engine, tables=[RegionalCompany.__table__])
    print("  ✓  created regional_companies (or already existed)")

    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM regional_companies")).scalar()
        in_radius = conn.execute(
            text("SELECT COUNT(*) FROM regional_companies WHERE in_radius")
        ).scalar()
        print(f"  ✓  {total} companies stored ({in_radius} within 50 km)")
        if total == 0:
            print("     → populate with: python3 scripts/rc_import_sheet.py "
                  "--csv <your exported sheet>.csv")


if __name__ == "__main__":
    print("Running Phase RC-1 migration (regional_companies table)...")
    run()
    print("Done.")
