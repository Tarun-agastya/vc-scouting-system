"""
Centralised write path: PostgreSQL first, then Qdrant.

Both rss_parser and web_scraper call upsert_startup() here.
This is the single place that decides:
  - Is this a new startup or a known one?
  - Which fields to fill / update?
  - How to append source attribution?
  - What stable ID to use for Qdrant?
"""
import logging
from datetime import datetime
from typing import Optional

from processing.deduplicator import (
    fuzzy_match_existing,
    generate_fingerprint,
    name_to_stable_uuid,
    normalize_company_name,
)

logger = logging.getLogger(__name__)


def upsert_startup(
    startup: dict,
    source: str,
    source_url: str,
    published_date: Optional[str] = None,
) -> Optional[str]:
    """
    Write a startup record to PostgreSQL, then sync to Qdrant.

    Logic:
      - Compute fingerprint from normalized name + domain.
      - If a row with that fingerprint already exists:
          • Append to source_history (skip if URL already recorded).
          • Fill any NULL fields with newly discovered values.
      - If new: insert a complete row.
      - Always re-embed and upsert to Qdrant with the stable UUID.

    Returns the stable UUID string, or None if the record was skipped
    (e.g. empty name).
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from sqlalchemy.orm.attributes import flag_modified

    name = (startup.get("name") or "").strip()
    if not name or len(name) < 2:
        return None

    website = startup.get("website") or ""
    fingerprint = generate_fingerprint(name, website)
    stable_id = name_to_stable_uuid(name, website)

    if not fingerprint or not stable_id:
        return None

    source_entry = {
        "source": source,
        "url": source_url,
        "date": published_date or datetime.utcnow().isoformat(),
    }

    db = SessionLocal()
    try:
        existing = (
            db.query(Startup).filter(Startup.fingerprint == fingerprint).first()
        )

        # Phase 2 fallback: fuzzy name match for variants with a different
        # domain (or no website).  Only runs when fingerprint yields nothing.
        if not existing:
            fuzzy = fuzzy_match_existing(name, db)
            if fuzzy:
                fuzzy_id, fuzzy_name, score = fuzzy
                existing = (
                    db.query(Startup)
                    .filter(Startup.id == fuzzy_id)
                    .first()
                )
                logger.info(
                    f"[Storage] Fuzzy match '{name}' → '{fuzzy_name}' "
                    f"(score={score}) — treating as same startup"
                )

        if existing:
            # ── Update existing record ────────────────────────────────────────
            # Evaluate rescore triggers BEFORE mutating source_history so
            # should_rescore() sees the pre-mutation URL set correctly.
            from processing.scorer import compute_enrichment_score, should_rescore
            do_score = should_rescore(existing, source_url)

            history: list = list(existing.source_history or [])
            known_urls = {entry.get("url") for entry in history}
            if source_url not in known_urls:
                history.append(source_entry)
                existing.source_history = history
                flag_modified(existing, "source_history")

            _fill_empty_fields(existing, startup)
            existing.updated_at = datetime.utcnow()
            db.commit()
            record_id     = str(existing.id)
            active_record = existing
            logger.debug(f"[Storage] Updated existing record: {name}")

        else:
            # ── Insert new record ─────────────────────────────────────────────
            contact_raw = startup.get("contact_info") or ""
            linkedin_val = contact_raw if "linkedin.com" in contact_raw else None

            new_startup = Startup(
                id=stable_id,
                name=name,
                normalized_name=normalize_company_name(name),
                fingerprint=fingerprint,
                description=startup.get("description"),
                website=website or None,
                contact_info=contact_raw or None,
                linkedin=linkedin_val,
                industry=startup.get("industry"),
                sub_industry=startup.get("sub_industry"),
                country=startup.get("country"),
                city=startup.get("city"),
                funding_stage=startup.get("funding_stage"),
                founded_year=_safe_int(startup.get("founded_year")),
                tags=startup.get("tags") or [],
                source=source,
                source_url=source_url,
                source_history=[source_entry],
                published_at=_parse_date(published_date),
                raw_data=startup,
            )
            db.add(new_startup)
            db.commit()
            record_id     = stable_id
            active_record = new_startup
            do_score      = True
            logger.debug(f"[Storage] Inserted new record: {name}")

        # ── Scoring ───────────────────────────────────────────────────────────
        if do_score:
            from processing.scorer import compute_enrichment_score
            score_result = compute_enrichment_score(active_record)
            active_record.enrichment_score  = score_result.enrichment_score
            active_record.source_confidence = score_result.source_confidence
            active_record.score_tier        = score_result.score_tier
            active_record.score_breakdown   = score_result.score_breakdown
            active_record.last_enriched_at  = datetime.utcnow()
            flag_modified(active_record, "score_breakdown")
            db.commit()
            logger.info(
                f"[Scorer] '{name}': {score_result.enrichment_score}/100 "
                f"({score_result.score_tier}), confidence={score_result.source_confidence}"
            )

        # ── Sync to Qdrant with stable ID ─────────────────────────────────────
        embed_text = embedder.build_startup_text(startup)
        vector = embedder.embed(embed_text)

        qdrant_payload = {
            **startup,
            "id":               record_id,
            "fingerprint":      fingerprint,
            "source":           source,
            "source_url":       source_url,
            "published_date":   published_date,
            "enrichment_score": active_record.enrichment_score  or 0.0,
            "source_confidence": active_record.source_confidence or 0.0,
            "score_tier":       active_record.score_tier        or "WEAK_SIGNAL",
        }
        qdrant_store.upsert_startup(record_id, vector, qdrant_payload)

        return record_id

    except Exception as exc:
        logger.error(f"[Storage] Failed to store '{name}': {exc}")
        db.rollback()
        return None
    finally:
        db.close()


# ── Private helpers ───────────────────────────────────────────────────────────

def _fill_empty_fields(existing, new_data: dict) -> None:
    """
    Copy fields from new_data into an existing Startup row only when the
    current value is None or empty.  Never overwrites populated data.
    """
    field_map = {
        "description": "description",
        "website": "website",
        "industry": "industry",
        "sub_industry": "sub_industry",
        "country": "country",
        "city": "city",
        "funding_stage": "funding_stage",
        "contact_info": "contact_info",
        "linkedin": "linkedin",
    }
    for model_field, dict_key in field_map.items():
        current = getattr(existing, model_field, None)
        if not current and new_data.get(dict_key):
            setattr(existing, model_field, new_data[dict_key])

    # founded_year: only fill if missing
    if not existing.founded_year and new_data.get("founded_year"):
        existing.founded_year = _safe_int(new_data["founded_year"])

    # tags: merge lists (no duplicates)
    if new_data.get("tags"):
        current_tags = set(existing.tags or [])
        new_tags = set(new_data["tags"])
        if new_tags - current_tags:
            existing.tags = list(current_tags | new_tags)


def _safe_int(value) -> Optional[int]:
    """Convert to int safely; return None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 date string; return None on failure."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except Exception:
        return None
