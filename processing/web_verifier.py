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
from urllib.parse import urlparse

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

# Same field set H-3's Layer 2 judges (processing/verifier.py::_RECHECK_FIELDS)
# minus name, which identity_match and the search query already cover.
# website WAS also excluded (comment used to read "minus name/website") —
# added back in Phase P-2 (29 Jul): it's almost always empty in storage
# (nothing ever populated it), and it's exactly the kind of fact web search
# is good at finding. _is_official_website() below guards against a
# LinkedIn/Crunchbase/etc. profile page being mistaken for the real site.
_CHECK_FIELDS = [
    "short_description", "description", "industry", "sub_industry",
    "tech_cluster", "country", "city", "address", "funding_stage",
    "founded_year", "employee_count", "contact_info", "website",
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
    "homepage": "website",
    "url": "website",
    "domain": "website",
}

# News/directory sites that aren't in settings.dedup_multitenant_domains
# (that list is tuned for identity-matching, not "is this a company's own
# site" — e.g. munich-startup.de is a single-tenant domain by the matcher's
# definition, but it's still never any given startup's OWN homepage).
_NON_OFFICIAL_WEBSITE_EXTRA = {
    "pitchbook.com", "tracxn.com", "dealroom.co", "wikipedia.org",
    "techcrunch.com", "sifted.eu", "tech.eu", "wellfound.com",
    "startupticker.ch", "munich-startup.de", "gruenderszene.de",
}


def _is_official_website(url: str) -> bool:
    """
    True if `url` looks like a company's own homepage rather than a profile
    page on someone else's platform. Reuses the dedup matcher's multi-tenant
    blocklist (settings.dedup_multitenant_domains, processing/matcher.py) —
    a domain that can't identify a unique company for dedup purposes can't
    be that company's homepage either — plus a few news/directory sites
    specific to this "is it really their own site" check.
    """
    from config import settings

    if not url:
        return False
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    blocklist = (
        {d.strip().lower() for d in settings.dedup_multitenant_domains.split(",") if d.strip()}
        | _NON_OFFICIAL_WEBSITE_EXTRA
    )
    return not any(host == d or host.endswith("." + d) for d in blocklist)


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


def build_proposal(record, verdict: dict) -> dict:
    """
    Turn a verdict's raw `findings` into the same {field: {old, new,
    source_url}} shape apply_verdict stages — the field-name normalization,
    no-op/unchanged filtering, and the website official-domain guard, all in
    one place, but WITHOUT writing anything. Factored out (Phase P-3, 29 Jul)
    so the per-startup "Verify now" endpoint can show a human the proposed
    changes before anything is written, using the exact same rules
    apply_verdict's batch path uses — one place decides what a finding is
    allowed to become, not two.

    Returns {field: {old, new, source_url}}, empty if nothing survives.
    """
    findings = [
        f for f in (verdict.get("findings") or [])
        if f.get("verdict") == "contradicted" and f.get("field")
    ]
    proposed = {}
    for f in findings:
        attr = _normalize_field(f["field"])
        if attr not in _CHECK_FIELDS:
            continue
        old_val = getattr(record, attr, None)
        new_val = f.get("correct_value")
        if not new_val or str(new_val).strip() == str(old_val or "").strip():
            continue
        if attr == "website" and not _is_official_website(new_val):
            logger.warning(
                f"[WebVerifier] Rejected non-official website proposal for "
                f"'{record.name}': {new_val!r} (aggregator/social/news domain)"
            )
            continue
        proposed[attr] = {"old": old_val, "new": new_val, "source_url": f.get("source_url")}
    return proposed


def apply_verdict(db, record, results: list, verdict: dict) -> str:
    """
    Apply one already-computed web-verify verdict to a record: stage a
    field_update review on a real contradiction, mark 'verified' on a clean
    check, or leave 'flagged'/'identity_unconfirmed' if the search results
    couldn't even confirm this is the right company. Never applies a
    correction directly — always stages for human approval, same
    S-3b stewardship contract as every other pipeline path.

    Factored out of web_verify_pending's loop body (27 Jul) so a verdict
    already computed elsewhere — e.g. an ad-hoc verification pass run
    outside the normal no_source_excerpt-only backlog — can be staged
    through the exact same, tested path instead of duplicating this logic.

    Returns the outcome key: "unchanged" | "verified" | "staged".
    """
    from processing.storage import _create_review

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
        return "unchanged"

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
        return "verified"

    proposed = build_proposal(record, verdict)
    for attr in proposed:
        proposed[attr]["incoming_source"] = "web_verification"
        proposed[attr]["incoming_extracted_at"] = datetime.utcnow().isoformat()

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
        return "staged"

    record.verification_status = "verified"
    record.verification_notes = verdict.get("summary") or "Confirmed via web search."
    record.verification_evidence = {
        "reason": "web_verified", "web_verdict": verdict,
        "search_results": results,
    }
    record.verified_at = datetime.utcnow()
    db.commit()
    return "verified"


async def web_verify_pending(limit: int = 15, progress=None) -> dict:
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
        if progress is not None:
            progress.total = len(candidates)

        loop = asyncio.get_event_loop()
        ollama_down = False
        consecutive_timeouts = 0
        # Require several timeouts IN A ROW before concluding Ollama itself
        # is down, not just one slow record — see processing/verifier.py's
        # identical fix (27 Jul) for the full story: one outlier timeout
        # used to abandon the entire rest of a multi-hundred-record batch.
        # web_verify_record already retries once internally, so this is a
        # second line of defense, not the primary fix.
        CONSECUTIVE_TIMEOUT_THRESHOLD = 3

        for record in candidates:
            if progress is not None:
                progress.current_name = record.name
            try:
                results = await loop.run_in_executor(None, _search_record, record)
                if not results:
                    counts["unchanged"] += 1
                    continue

                if ollama_down:
                    counts["unchanged"] += 1
                    continue

                verdict = await loop.run_in_executor(None, _verify_record, record, results)
                consecutive_timeouts = 0

                outcome = apply_verdict(db, record, results, verdict)
                counts[outcome] += 1

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[WebVerifier] Failed for {record.id} ({record.name}): {exc}"
                )
                counts["errors"] += 1
                msg = str(exc).lower()
                if any(s in msg for s in ("connect", "timeout", "timed out", "refused", "unreachable")):
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= CONSECUTIVE_TIMEOUT_THRESHOLD:
                        ollama_down = True
                        logger.warning(
                            f"[WebVerifier] {CONSECUTIVE_TIMEOUT_THRESHOLD} consecutive timeouts — "
                            f"treating Ollama as down for the rest of this batch"
                        )
                else:
                    consecutive_timeouts = 0
            finally:
                if progress is not None:
                    progress.processed += 1

        logger.info(f"[WebVerifier] Batch: {counts}")
        return counts
    finally:
        db.close()
