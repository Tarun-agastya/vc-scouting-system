"""
Async LLM explanation for staged reviews (Phase S-3b, Layer 4).

The local reasoning model (qwen3:14b) reads the evidence for a pending review
and writes a plain-language *explanation* — never a yes/no verdict, never a
status change. It only makes the human's decision faster.

Runs one item at a time under the ScoutController GPU mutex, so it never
collides with the extraction pipeline. Intended to be called from the nightly
scheduler job (in-process, shares the mutex) — see api/main.py. A thin CLI
wrapper (scripts/llm_explain.py) exists for manual off-hours runs.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def _build_prompt(review) -> str:
    ev = review.evidence or {}
    ev_lines = "\n".join(f"  - {k}: {v}" for k, v in ev.items())

    if review.review_type == "field_update":
        changes = "\n".join(
            f"  - {f}: '{c.get('old')}'  →  '{c.get('new')}'  (from {c.get('incoming_source')})"
            for f, c in (review.proposed_changes or {}).items()
        )
        return (
            "A data-review system found that new information about an existing startup "
            f"('{review.master_name}') differs from what we already have. Explain in 1-2 "
            "plain sentences whether the change looks trustworthy and what a reviewer should "
            "check. Do NOT give a yes/no decision.\n\n"
            f"Proposed changes:\n{changes}\n\nMatch evidence:\n{ev_lines}"
        )

    return (
        "A data-review system flagged that an incoming startup record might be the same "
        f"company as an existing one ('{review.master_name}' vs incoming '{review.incoming_name}'). "
        "Explain in 1-2 plain sentences why they might or might not be the same, citing the "
        "strongest evidence. Do NOT give a yes/no decision.\n\n"
        f"Match evidence:\n{ev_lines}"
    )


async def explain_pending_reviews(limit: int = 30) -> int:
    """
    Fill `llm_explanation` on up to `limit` pending reviews that don't have one.
    Returns the number explained. Never raises; degrades cleanly if Ollama is down.
    """
    from database.connection import SessionLocal
    from database.models import DuplicateReview
    from reasoning.qwen_client import qwen_client
    from processing.scout_controller import scout_controller

    db = SessionLocal()
    try:
        pending = (
            db.query(DuplicateReview)
            .filter(DuplicateReview.status == "pending",
                    DuplicateReview.llm_explanation.is_(None))
            .order_by(DuplicateReview.created_at.asc())
            .limit(limit)
            .all()
        )
        if not pending:
            return 0

        loop = asyncio.get_event_loop()
        done = 0
        for review in pending:
            prompt = _build_prompt(review)
            try:
                # One at a time under the GPU mutex — never fights ingestion.
                async with scout_controller.gpu_mutex:
                    text = await loop.run_in_executor(None, qwen_client.generate, prompt)
                review.llm_explanation = (text or "").strip()[:2000]
                db.commit()
                done += 1
            except Exception as exc:
                db.rollback()
                logger.warning(f"[LLM-Explain] Failed for review {review.id}: {exc}")
                # Stop the batch if the model is unreachable — retry next run.
                break

        logger.info(f"[LLM-Explain] Explained {done}/{len(pending)} pending review(s)")
        return done
    finally:
        db.close()
