"""
Phase R-2 migration (31 Jul — self-adapting web extraction).

Creates the `site_profiles` table: the pipeline's LEARNED per-source
extraction strategy, kept separate from human-authored config/sources.yaml.
See database/models.py::SiteProfile for the full column reference and
processing/site_profile_store.py for the lookup logic.

The table starts empty — nothing has been probed yet. Phase R-2's dashboard
ships a "Profile all sources" batch action that populates it for every
registered source in one pass.

Safe to run multiple times (CREATE TABLE/INDEX IF NOT EXISTS via
create_all's checkfirst, same as scripts/migrate_reviews.py).

Usage:
    python scripts/migrate_site_profiles.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import engine
from database.models import Base, SiteProfile


def run():
    Base.metadata.create_all(bind=engine, tables=[SiteProfile.__table__])
    print("  ✓  created site_profiles (or already existed)")

    with engine.connect() as conn:
        from sqlalchemy import text
        total = conn.execute(text("SELECT COUNT(*) FROM site_profiles")).scalar()
        print(f"  ✓  {total} profile(s) currently stored — use the dashboard's "
              f"\"Profile all sources\" button (or scripts/inspect_site.py --all) "
              f"to populate it")


if __name__ == "__main__":
    print("Running Phase R-2 migration (site_profiles table)...")
    run()
    print("Done.")
