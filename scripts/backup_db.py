"""
Take a Postgres + Qdrant backup now. Normally runs nightly at 00:30 from the
API scheduler (api/main.py); this is the manual trigger.

Restore (Postgres):
    gunzip -c backups/nightly/<date>/postgres.sql.gz | \\
        docker exec -i vc_postgres psql -U scout -d vc_scouting

Usage:
    python3 scripts/backup_db.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from processing.backup import latest_backup_age_hours, run_backup
from processing.review_actions import record_resolver_run


if __name__ == "__main__":
    started = datetime.utcnow()
    stats = run_backup()
    record_resolver_run("backup", stats, started)
    for k, v in stats.items():
        print(f"  {k:16} {v}")
    if "error" in stats:
        sys.exit(1)
    print(f"\nLatest complete backup: {latest_backup_age_hours():.2f}h old")
