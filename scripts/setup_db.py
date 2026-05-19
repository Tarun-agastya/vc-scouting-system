"""
Setup script: initialize PostgreSQL tables and Qdrant collections.
Run once before starting the API for the first time.

Usage:
    python scripts/setup_db.py
"""
import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def main():
    print("=" * 60)
    print("  VC Scouting Intelligence System — Database Setup")
    print("=" * 60)

    # 1. PostgreSQL
    print("\n[1/2] Initializing PostgreSQL tables...")
    try:
        from database.connection import init_db
        init_db()
        print("      PostgreSQL tables created.")
    except Exception as exc:
        print(f"      ERROR: {exc}")
        print("      Make sure Docker is running: docker-compose up -d")
        sys.exit(1)

    # 2. Qdrant
    print("\n[2/2] Initializing Qdrant collections...")
    try:
        from vector_db.qdrant_store import qdrant_store
        qdrant_store.ensure_collections()
        print("      Qdrant collections ready.")
    except Exception as exc:
        print(f"      ERROR: {exc}")
        print("      Make sure Qdrant is running: docker-compose up -d")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  Setup complete! You can now start the API:")
    print("  python -m uvicorn api.main:app --reload --port 8000")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
