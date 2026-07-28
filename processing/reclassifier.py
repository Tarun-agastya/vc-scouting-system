"""
Controlled-taxonomy reclassification pass (Phase V-2, 27 Jul).

Extraction's free-form industry/tech_cluster produced inconsistent, noisy
values (e.g. "Proptech - Fashion Tech") — this module re-classifies every
startup into config/taxonomy.yaml's controlled vocabulary via
qwen_client.classify_startup(), the same taxonomy-constrained call new
ingests now go through at insert time (processing/storage.py::_insert_master).

Startup.classified_at is the resumability marker: NULL means "still holds
whatever free-form value extraction produced, or ingest-time classification
failed" — the whole existing backlog (~660 records) starts this way after
scripts/migrate_classification.py, per the owner's "reclassify all now"
decision. A record is only ever picked up once; if Ollama dies mid-batch the
remaining records simply stay NULL for the next run — no data loss, no
guessing.

The original free-form industry/tech_cluster values need no special
preservation step here: every row's raw_data already holds them (raw_data is
set to the full extraction dict at insert time and is never touched by this
pass), so overwriting Startup.industry/.tech_cluster loses nothing.

Called exclusively through processing.scout_controller.run_reclassify(),
which wraps the whole batch in the GPU mutex via ScoutController._execute() —
do NOT acquire scout_controller.gpu_mutex again inside this module (mirrors
processing/verifier.py's same rule; asyncio.Lock isn't reentrant).
"""
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _classify_record(record) -> dict:
    """Synchronous — dispatched via run_in_executor by the caller."""
    from reasoning.qwen_client import qwen_client
    return qwen_client.classify_startup(
        record.name, record.short_description or "", record.description or ""
    )


async def reclassify_pending(limit: int = 20, progress=None) -> dict:
    """
    Re-classify up to `limit` startups whose classified_at is still NULL.

    Returns {"classified": n, "errors": n}. Never raises. If Ollama fails
    partway through, remaining records in this batch are left with
    classified_at=NULL for the next run — not guessed at.

    `progress`: optional scout_controller.RecordProgress — updated per
    record so /ingestion/status shows real "processed / total" counters
    while running.
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from api.routes.reviews import _reindex

    db = SessionLocal()
    counts = {"classified": 0, "errors": 0}
    try:
        pending = (
            db.query(Startup)
            .filter(Startup.classified_at.is_(None))
            .order_by(Startup.created_at.asc())
            .limit(limit)
            .all()
        )
        if progress is not None:
            progress.total = len(pending)

        loop = asyncio.get_event_loop()
        ollama_down = False
        consecutive_timeouts = 0
        # Require several timeouts IN A ROW before concluding Ollama itself
        # is down, not just one slow record — see processing/verifier.py's
        # identical fix (27 Jul) for the full story: one outlier timeout
        # used to abandon the entire rest of a multi-hundred-record batch.
        # classify_startup already retries once internally, so this is a
        # second line of defense, not the primary fix.
        CONSECUTIVE_TIMEOUT_THRESHOLD = 3

        for record in pending:
            if progress is not None:
                progress.current_name = record.name
            try:
                if ollama_down:
                    continue  # leave classified_at NULL — retried on the next run

                result = await loop.run_in_executor(None, _classify_record, record)

                record.industry = result.get("industry")
                record.tech_cluster = result.get("tech_cluster")
                record.classified_at = datetime.utcnow()

                _reindex(db, record)  # re-score/re-embed — commits internally
                counts["classified"] += 1
                consecutive_timeouts = 0

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[Reclassifier] Failed for {record.id} ({record.name}): {exc}"
                )
                counts["errors"] += 1
                msg = str(exc).lower()
                if any(s in msg for s in ("connect", "timeout", "timed out", "refused", "unreachable")):
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= CONSECUTIVE_TIMEOUT_THRESHOLD:
                        ollama_down = True
                        logger.warning(
                            f"[Reclassifier] {CONSECUTIVE_TIMEOUT_THRESHOLD} consecutive timeouts — "
                            f"treating Ollama as down for the rest of this batch"
                        )
                else:
                    consecutive_timeouts = 0
            finally:
                if progress is not None:
                    progress.processed += 1

        logger.info(f"[Reclassifier] Batch: {counts}")
        return counts
    finally:
        db.close()
