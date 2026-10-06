"""Adds decision_audits.source ('live' | 'backtest'). Existing rows are live.
Idempotent.   python3 scripts/migrate_decision_audit_source.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text

from database.connection import engine

if __name__ == "__main__":
    with engine.begin() as c:
        c.execute(text("ALTER TABLE decision_audits ADD COLUMN IF NOT EXISTS source VARCHAR(12) DEFAULT 'live'"))
        c.execute(text("UPDATE decision_audits SET source='live' WHERE source IS NULL"))
    print("  ✓  decision_audits.source present")
