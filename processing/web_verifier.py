"""
Web-search verification for the no-source_excerpt backlog (Phase W, 23 Jul).

H-3's recheck (processing/verifier.py) can only check a record against its
own stored source_excerpt — Phase A of recheck_pending() immediately flags
anything with no excerpt as "no_source_excerpt" because there's nothing to
check it against. That covers 345 of 353 records (everything ingested before
21 Jul, when H-1 started capturing excerpts). This module gives that backlog
an automated path to real verification: live web search stands in for the
missing source_excerpt as the evidence base.

Same "evidence-gathering machine, never a guessing machine" contract as the
rest of this pipeline (S-3b): a finding is never applied directly to a
master. Every contradiction becomes a staged field_update review — exactly
the shape a human produces doing this by hand (the 23 Jul manual pass on 9
records is the reference implementation this automates) — with
incoming_source="web_verification" and a source_url cited per finding.

Called exclusively through processing.scout_controller.run_web_verify(),
which wraps the whole batch in the GPU mutex via ScoutController._execute()
for the LLM step — do NOT acquire scout_controller.gpu_mutex again inside
this module (asyncio.Lock is not reentrant). The search calls themselves are
plain network I/O, not mutex-bound.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

# Same field set H-3's Layer 2 judges (processing/verifier.py::_RECHECK_FIELDS)
# minus name/website, which identity_match and the search query already cover.
_CHECK_FIELDS = [
    "short_description", "description", "industry", "sub_industry",
    "tech_cluster", "country", "city", "address", "funding_stage",
    "founded_year", "employee_count", "contact_info",
]

# Maps a web-verify finding's "field" (matched loosely against what the model
# returns) to the real Startup attribute name, so a staged field_update uses
# a name _apply_field_updates() (api/routes/reviews.py) actually recognizes.
_FIELD_ALIASES = {
    "one_liner": "short_description",
    "founded": "founded_year",
    "year_founded": "founded_year",
    "headquarters": "city",
    "location": "city",
}


def _build_search_query(record) -> str:
    parts = [record.name]
    if record.city:
        parts.append(record.city)
    elif record.country:
        parts.append(record.country)
    parts.append("startup founded")
    return " ".join(parts)


def _format_search_results(results: list) -> str:
    if not results:
        return "(no results found)"
    lines = []
    for r in results:
        lines.append(f"- {r['title']}\n  URL: {r['url']}\n  \"{r['snippet']}\"")
    return "\n".join(lines)


def _build_verification_prompt(record, results: list) -> str:
    from reasoning.prompts import WEB_VERIFICATION_PROMPT

    field_lines = "\n".join(
        f"  - {f}: {getattr(record, f, None)!r}" for f in _CHECK_FIELDS
    )
    context = record.city or record.country or "location unknown"
    return WEB_VERIFICATION_PROMPT.format(
        name=record.name,
        context=context,
        search_results=_format_search_results(results),
        fields=field_lines,
    )


def _search_record(record) -> list:
    """Synchronous — dispatched via run_in_executor by the caller."""
    from ingestion.web_search import search
    return search(_build_search_query(record), max_results=5)


def _verify_record(record, results: list) -> dict:
    """Synchronous — dispatched via run_in_executor by the caller."""
    from reasoning.qwen_client import qwen_client
    prompt = _build_verification_prompt(record, results)
    return qwen_client.web_verify_record(prompt)


def _normalize_field(field: str) -> str:
    f = (field or "").strip().lower().replace(" ", "_")
    return _FIELD_ALIASES.get(f, f)


async def web_verify_pending(limit: int = 15) -> dict:
    """
    Process up to `limit` records from the no_source_excerpt backlog:
    verification_status='flagged' with verification_evidence.reason ==
    'no_source_excerpt' (exactly Phase A's output in processing/verifier.py).

    Per record: web search -> LLM verdict against the results ->
      - identity_match false or a real contradiction found -> stage a
        field_update review per contradicted field (never applied directly),
        verification_notes updated, status stays 'flagged' (now with a real
        reason, not just "no excerpt").
      - no contradictions, identity confirmed -> verification_status='verified'.
      - no usable search results at all -> left untouched, retried next batch.

    Returns {"verified": n, "staged": n, "unchanged": n, "errors": n}.
    Never raises. If Ollama fails partway through, remaining records in this
    batch are left as-is (retried on the next run) rather than guessed at.
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from processing.storage import _create_review

    db = SessionLocal()
    counts = {"verified": 0, "staged": 0, "unchanged": 0, "errors": 0}
    try:
        # The reason filter (only records Phase A actually parked here, not a
        # human-flagged H-3 verdict that happens to also lack an excerpt) MUST
        # be applied in SQL, before LIMIT — not in Python after fetching the
        # top N by score. Found live 24 Jul: once a batch of high-score
        # records had each been touched once (reason changed away from
        # "no_source_excerpt" to e.g. "web_verification_flagged"), they still
        # matched the outer flagged+no-excerpt filter and kept winning the
        # ORDER BY score / LIMIT, so every subsequent run fetched the SAME
        # already-processed handful, filtered them all out, and returned all
        # zeros — while 229 genuinely untouched records sat unreachable
        # further down the list because they never won the LIMIT cutoff.
        candidates = (
            db.query(Startup)
            .filter(Startup.verification_status == "flagged")
            .filter(Startup.source_excerpt.is_(None) | (Startup.source_excerpt == ""))
            .filter(cast(Startup.verification_evidence, JSONB)["reason"].astext == "no_source_excerpt")
            .order_by(Startup.enrichment_score.desc().nullslast())
            .limit(limit)
            .all()
        )

        loop = asyncio.get_event_loop()
        ollama_down = False

        for record in candidates:
            try:
                results = await loop.run_in_executor(None, _search_record, record)
                if not results:
                    counts["unchanged"] += 1
                    continue

                if ollama_down:
                    counts["unchanged"] += 1
                    continue

                verdict = await loop.run_in_executor(None, _verify_record, record, results)

                identity_ok = verdict.get("identity_match", True)
                findings = [
                    f for f in (verdict.get("findings") or [])
                    if f.get("verdict") == "contradicted" and f.get("field")
                ]

                if not identity_ok:
                    record.verification_status = "flagged"
                    record.verification_notes = (
                        f"Web verification could not confirm this is the right company: "
                        f"{verdict.get('summary') or ''}"
                    )
                    record.verification_evidence = {
                        "reason": "identity_unconfirmed", "web_verdict": verdict,
                        "search_results": results,
                    }
                    record.verified_at = datetime.utcnow()
                    db.commit()
                    counts["unchanged"] += 1
                    continue

                if not findings:
                    record.verification_status = "verified"
                    record.verification_notes = (
                        verdict.get("summary") or "Confirmed via web search — no contradictions found."
                    )
                    record.verification_evidence = {
                        "reason": "web_verified", "web_verdict": verdict,
                        "search_results": results,
                    }
                    record.verified_at = datetime.utcnow()
                    db.commit()
                    counts["verified"] += 1
                    continue

                proposed = {}
                for f in findings:
                    attr = _normalize_field(f["field"])
                    if attr not in _CHECK_FIELDS:
                        continue
                    old_val = getattr(record, attr, None)
                    new_val = f.get("correct_value")
                    if not new_val or str(new_val).strip() == str(old_val or "").strip():
                        continue
                    proposed[attr] = {
                        "old": old_val, "new": new_val,
                        "incoming_source": "web_verification",
                        "incoming_extracted_at": datetime.utcnow().isoformat(),
                        "source_url": f.get("source_url"),
                    }

                if proposed:
                    _create_review(
                        db,
                        review_type="field_update",
                        master=record,
                        incoming_row=None,
                        incoming_data={"name": record.name},
                        proposed_changes=proposed,
                        evidence={"web_verdict": verdict, "search_results": results},
                        risk_level="high",
                        confidence=None,
                        source="web_verification",
                        run_id=None,
                    )
                    record.verification_notes = verdict.get("summary") or ""
                    record.verification_evidence = {
                        "reason": "web_verification_flagged", "web_verdict": verdict,
                        "search_results": results,
                    }
                    record.verified_at = datetime.utcnow()
                    db.commit()
                    counts["staged"] += 1
                else:
                    record.verification_status = "verified"
                    record.verification_notes = (
                        verdict.get("summary") or "Confirmed via web search."
                    )
                    record.verification_evidence = {
                        "reason": "web_verified", "web_verdict": verdict,
                        "search_results": results,
                    }
                    record.verified_at = datetime.utcnow()
                    db.commit()
                    counts["verified"] += 1

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[WebVerifier] Failed for {record.id} ({record.name}): {exc}"
                )
                counts["errors"] += 1
                msg = str(exc).lower()
                if any(s in msg for s in ("connect", "timeout", "timed out", "refused", "unreachable")):
                    ollama_down = True

        logger.info(f"[WebVerifier] Batch: {counts}")
        return counts
    finally:
        db.close()
