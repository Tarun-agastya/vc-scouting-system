"""
Manual ingestion runner — use this to populate the database on demand.

Usage:
    python scripts/run_ingestion.py [rss|accelerators|universities|all]

Examples:
    python scripts/run_ingestion.py rss           # RSS feeds only (fast)
    python scripts/run_ingestion.py accelerators  # Accelerator pages
    python scripts/run_ingestion.py all           # Everything (slow but thorough)
"""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def run_rss():
    from ingestion.rss_parser import rss_parser
    print("Running RSS feed ingestion...")
    startups = rss_parser.ingest_feeds(max_entries=50)
    print(f"Done. Extracted {len(startups)} startups from RSS feeds.")


def run_accelerators():
    import asyncio
    from config.source_registry import get_high_priority_sources, SourceType
    from ingestion.web_scraper import web_scraper

    # All HIGH priority non-university sources (accelerators, incubators, networks)
    sources = [
        s for s in get_high_priority_sources()
        if s.source_type != SourceType.UNIVERSITY_HUB
    ]
    print(f"Scraping {len(sources)} accelerator/hub pages (source registry)...")
    for source in sources:
        print(f"  [SOURCE] {source.source_name}")
        asyncio.run(web_scraper.scrape_source(source.primary_url, source.source_type.value))
    print("Accelerator/hub scraping complete.")


def run_universities():
    import asyncio
    from config.source_registry import get_high_priority_sources, SourceType
    from ingestion.web_scraper import web_scraper

    sources = [
        s for s in get_high_priority_sources()
        if s.source_type == SourceType.UNIVERSITY_HUB
    ]
    print(f"Scraping {len(sources)} university hub pages (source registry)...")
    for source in sources:
        print(f"  [SOURCE] {source.source_name}")
        asyncio.run(web_scraper.scrape_source(source.primary_url, source.source_type.value))
    print("University hub scraping complete.")


def run_all():
    run_rss()
    run_accelerators()
    run_universities()


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "rss"

    print("=" * 60)
    print(f"  VC Scouting — Ingestion Pipeline [{mode}]")
    print("=" * 60 + "\n")

    dispatch = {
        "rss":          run_rss,
        "accelerators": run_accelerators,
        "universities": run_universities,
        "all":          run_all,
    }

    func = dispatch.get(mode)
    if not func:
        print(f"Unknown mode '{mode}'. Choose: rss | accelerators | universities | all")
        sys.exit(1)

    func()

    # Show current DB size
    try:
        from vector_db.qdrant_store import qdrant_store
        count = qdrant_store.get_startup_count()
        print(f"\nTotal startups in vector DB: {count}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
