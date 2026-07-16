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
    extract_domain,
    generate_fingerprint,
    name_to_stable_uuid,
    normalize_company_name,
)

logger = logging.getLogger(__name__)


def _resolve_source_name(source_url: str) -> Optional[str]:
    """
    Best-effort human-readable label for a web/RSS source_url, looked up
    from the live registry (config/sources.yaml) by exact URL match.
    Newsletters (gmail://<id> URLs) never match here — their source_name
    comes from the explicit provenance dict instead.
    """
    try:
        from config.source_loader import get_web_sources, get_rss_feeds
        for s in get_web_sources():
            if s.primary_url == source_url:
                return s.source_name
        for f in get_rss_feeds():
            if f["url"] == source_url:
                return f["name"]
    except Exception:
        pass
    return None


def _get_current_run_id() -> Optional[str]:
    """
    Best-effort lookup of the scout_controller run currently in flight.
    None for calls outside a controller-managed run (e.g. manual /add-startup).
    """
    try:
        from processing.scout_controller import scout_controller
        return scout_controller.current_run_id
    except Exception:
        return None


def upsert_startup(
    startup: dict,
    source: str,
    source_url: str,
    published_date: Optional[str] = None,
    provenance: Optional[dict] = None,
) -> tuple[Optional[str], bool]:
    """
    Write a startup record to PostgreSQL, then sync to Qdrant.

    Logic:
      - Compute fingerprint from normalized name + domain.
      - If a row with that fingerprint already exists:
          • Append to source_history (skip if URL already recorded).
          • Fill any NULL fields with newly discovered values.
      - If new: insert a complete row.
      - Always re-embed and upsert to Qdrant with the stable UUID.

    provenance : optional dict with any of "source_name", "sender", "subject"
      — used by newsletter_ingestor to attach which newsletter (sender +
      subject) a startup came from, since gmail:// URLs alone aren't
      human-readable. Web/RSS callers don't need this — source_name is
      resolved automatically from the source registry by source_url.

    Returns (record_id, is_new):
      record_id — stable UUID string, or None on skip/error
      is_new    — True for a fresh INSERT, False for a dedup UPDATE
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from sqlalchemy.orm.attributes import flag_modified

    name = (startup.get("name") or "").strip()
    if not name or len(name) < 2:
        return None, False

    website = startup.get("website") or ""
    domain = extract_domain(website)
    # A fingerprint is only a reliable identity when a real domain backs it.
    # For no-website records we store NULL (Postgres allows repeated NULLs under
    # the UNIQUE constraint) and rely on the multi-signal matcher instead — this
    # is what lets two different same-named companies with no website coexist.
    fingerprint = generate_fingerprint(name, website) if domain else None
    stable_id = name_to_stable_uuid(name, website)  # deterministic candidate id

    if not stable_id:
        return None, False

    source_name = (provenance or {}).get("source_name") or _resolve_source_name(source_url)

    source_entry = {
        "source": source,
        "source_name": source_name,
        "url": source_url,
        "date": published_date or datetime.utcnow().isoformat(),
        "extracted_at": datetime.utcnow().isoformat(),
        "run_id": _get_current_run_id(),
    }
    if provenance:
        if provenance.get("sender"):
            source_entry["sender"] = provenance["sender"]
        if provenance.get("subject"):
            source_entry["subject"] = provenance["subject"]

    db = SessionLocal()
    try:
        # Embed the whole record once — reused for both the matcher's blocking
        # step and the final Qdrant sync below (avoids embedding twice).
        embed_text = embedder.build_startup_text(startup)
        incoming_vector = embedder.embed(embed_text)

        # Multi-signal match decision (Phase S-3): merge / review / new.
        from processing.matcher import find_match
        match = find_match(startup, db, incoming_vector)
        logger.info(
            f"[Storage] Match '{name}': {match.decision} "
            f"(conf={match.confidence}, {match.reason})"
        )

        existing = None
        if match.decision in ("merge", "review") and match.matched_id:
            existing = db.query(Startup).filter(Startup.id == match.matched_id).first()

        if match.decision == "merge" and existing:
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
            is_new        = False
            logger.debug(f"[Storage] Updated existing record: {name}")

        else:
            # ── Insert new record ─────────────────────────────────────────────
            contact_raw = startup.get("contact_info") or ""
            linkedin_val = contact_raw if "linkedin.com" in contact_raw else None

            # The deterministic stable_id is name-derived, so two *different*
            # same-named no-website companies would collide on it. If it's
            # already taken by a different row, mint a fresh id.
            insert_id = stable_id
            if db.query(Startup).filter(Startup.id == stable_id).first() is not None:
                import uuid as _uuid
                insert_id = str(_uuid.uuid4())

            new_startup = Startup(
                id=insert_id,
                name=name,
                normalized_name=normalize_company_name(name),
                fingerprint=fingerprint,
                short_description=startup.get("one_liner"),
                description=startup.get("description"),
                website=website or None,
                contact_info=contact_raw or None,
                linkedin=linkedin_val,
                industry=startup.get("industry"),
                sub_industry=startup.get("sub_industry"),
                tech_cluster=startup.get("tech_cluster"),
                country=startup.get("country"),
                city=startup.get("city"),
                address=startup.get("address"),
                funding_stage=startup.get("funding_stage"),
                founded_year=_safe_int(startup.get("founded_year")),
                employee_count=startup.get("employee_count"),
                tags=startup.get("tags") or [],
                source=source,
                source_url=source_url,
                source_history=[source_entry],
                published_at=_parse_date(published_date),
                raw_data=startup,
            )
            db.add(new_startup)
            db.commit()
            record_id     = insert_id
            active_record = new_startup
            do_score      = True
            is_new        = True
            logger.debug(f"[Storage] Inserted new record: {name}")

            # Uncertain match: keep the freshly-inserted startup (never lost),
            # but flag the possible-duplicate pair for human resolution.
            if match.decision == "review" and existing is not None:
                _create_review(db, new_startup, existing, match)

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
        vector = incoming_vector  # already computed above for the matcher

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

        return record_id, is_new

    except Exception as exc:
        logger.error(f"[Storage] Failed to store '{name}': {exc}")
        db.rollback()
        return None, False
    finally:
        db.close()


# ── Private helpers ───────────────────────────────────────────────────────────

def _create_review(db, incoming_row, existing_row, match) -> None:
    """
    Record an uncertain-match pair (Phase S-3) for human resolution in the
    dashboard. Never raises — a review-logging failure must not fail the
    upsert that already succeeded.
    """
    try:
        from database.models import DuplicateReview
        review = DuplicateReview(
            incoming_id=incoming_row.id,
            incoming_name=incoming_row.name,
            existing_id=existing_row.id,
            existing_name=existing_row.name,
            confidence=match.confidence,
            signals=match.signals,
            status="pending",
        )
        db.add(review)
        db.commit()
        logger.info(
            f"[Storage] Flagged possible duplicate: '{incoming_row.name}' ~ "
            f"'{existing_row.name}' (conf={match.confidence})"
        )
    except Exception as exc:
        db.rollback()
        logger.error(f"[Storage] Failed to record duplicate review: {exc}")


def _fill_empty_fields(existing, new_data: dict) -> None:
    """
    Copy fields from new_data into an existing Startup row only when the
    current value is None or empty.  Never overwrites populated data.
    """
    field_map = {
        "short_description": "one_liner",
        "description": "description",
        "website": "website",
        "industry": "industry",
        "sub_industry": "sub_industry",
        "tech_cluster": "tech_cluster",
        "country": "country",
        "city": "city",
        "address": "address",
        "funding_stage": "funding_stage",
        "employee_count": "employee_count",
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
