"""
Creates `decision_audits` — the ledger of whether humans agree with the
model, per field (A4 of the autonomy plan). See database/models.py::
DecisionAudit and processing/trust.py.

Starts empty; safe to re-run.   python3 scripts/migrate_decision_audit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import engine
from database.models import Base, DecisionAudit

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine, tables=[DecisionAudit.__table__])
    print("  ✓  created decision_audits (or already existed)")
