import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


def analyze_startup_batch(startups: List[Dict]) -> List[Dict]:
    """
    Run AI analysis on a list of startups.
    Adds 'ai_analysis' key to each. Failures are logged, not raised.
    """
    from reasoning.qwen_client import qwen_client

    results = []
    for startup in startups:
        try:
            analysis = qwen_client.analyze_startup(startup)
            startup["ai_analysis"] = analysis
        except Exception as exc:
            logger.error(f"[Analyzer] Failed for {startup.get('name')}: {exc}")
            startup["ai_analysis"] = None
        results.append(startup)
    return results


def generate_sector_report(sector: str, startups: List[Dict]) -> str:
    """Generate a full sector intelligence briefing."""
    from reasoning.qwen_client import qwen_client
    return qwen_client.generate_sector_report(sector, startups)


def enrich_startup_in_db(startup_id: str) -> bool:
    """
    Fetch a startup from PostgreSQL, run AI analysis, and save back.
    Returns True on success.
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from reasoning.qwen_client import qwen_client

    db = SessionLocal()
    try:
        startup = db.query(Startup).filter(Startup.id == startup_id).first()
        if not startup:
            logger.warning(f"[Analyzer] Startup not found: {startup_id}")
            return False

        data = {
            "name": startup.name,
            "industry": startup.industry,
            "description": startup.description,
            "city": startup.city,
            "country": startup.country,
            "funding_stage": startup.funding_stage,
            "website": startup.website,
        }

        analysis = qwen_client.analyze_startup(data)
        startup.ai_summary = analysis
        db.commit()
        return True

    except Exception as exc:
        logger.error(f"[Analyzer] Enrich failed for {startup_id}: {exc}")
        db.rollback()
        return False
    finally:
        db.close()
