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
rest of this pipeline (S-3b) for anything that CONFLICTS with an existing
value: never applied directly, always a staged field_update review — exactly
the shape a human produces doing this by hand (the 23 Jul manual pass on 9
records is the reference implementation this automates) — with
incoming_source="web_verification" and a source_url cited per finding.

EMPTY-FIELD FILLS ARE DIFFERENT (12 Aug 2026, owner): a field that was blank
before verification has nothing to conflict with — there's no existing
human judgment call being second-guessed, just a gap being filled with a
sourced fact. Those are applied directly to the record rather than staged,
mirroring the precedent already established in regional/enrich.py's
apply_proposals ("fill empties, never silently overwrite"). Only genuine
conflicts (a real, non-empty old value contradicted by a new one) still go
to the Review Inbox — see _split_proposal / apply_verdict below. This was
driven by the review queue being flooded with proposals that were actually
uncontested fills, not real disagreements to adjudicate.

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
# Fields web verification is allowed to check and propose corrections for.
#
# industry and tech_cluster are DELIBERATELY excluded (30 Jul). Since Phase
# V-2 both hold a CONTROLLED TAXONOMY value owned by classify_startup() /
# the reclassify pass — not free text. Left in this list, the web-verify
# model proposed replacing a valid taxonomy value with free-form prose
# (observed live on Clypp: "B2B SaaS & Enterprise Software" ->
# "Video-based knowledge transfer solutions"), which an approving human
# would then write straight into the taxonomy column, breaking thesis
# matching and the Browse industry filter. Same root cause as the
# _diff_fields flood fix in processing/storage.py the same day: a
# controlled field must never be diffed against, or overwritten by, a raw
# LLM value. sub_industry stays — it is still free-form on both sides.
_CHECK_FIELDS = [
    "short_description", "description", "sub_industry",
    "country", "city", "address", "funding_stage",
    "founded_year", "employee_count", "contact_info", "website",
]

# Free-text fields resolved deterministically via field_policy.better_freetext
# (Phase Z, 12 Aug 2026) rather than staged for a human — see that module's
# docstring for why: they were never competing facts, only different levels
# of detail, and length-based informativeness gets both directions right
# (an upgrade AND a downgrade) without a review click. Same set storage.py's
# _diff_fields carves out for the same reason.
_TEXT_FIELDS = {"short_description", "description"}

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


# Every terminal branch of apply_verdict() writes exactly one of these into
# verification_evidence["reason"]. That makes them the reliable "this record
# has already been through web verification" marker — the only one available,
# since the `staged` branch deliberately does NOT change verification_status.
# web_verify_new_stubs' selection depends on this staying exhaustive: adding a
# new terminal branch to apply_verdict without adding its reason here would
# silently make that query non-self-clearing again.
_WEB_VERIFIED_REASONS = ("web_verified", "web_verification_flagged", "identity_unconfirmed")


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
    from processing.field_policy import better_freetext

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
        if not new_val:
            continue

        if attr in _TEXT_FIELDS:
            winner = better_freetext(old_val, new_val)
            old_s = "" if old_val is None else str(old_val).strip()
            if winner is None or winner == old_s:
                continue  # no meaningful change, or old is already the richer value
            new_val = winner
        elif str(new_val).strip() == str(old_val or "").strip():
            continue

        if attr == "website" and not _is_official_website(new_val):
            logger.warning(
                f"[WebVerifier] Rejected non-official website proposal for "
                f"'{record.name}': {new_val!r} (aggregator/social/news domain)"
            )
            continue
        proposed[attr] = {"old": old_val, "new": new_val, "source_url": f.get("source_url")}
    return proposed


def apply_verdict(db, record, results: list, verdict: dict, run_id=None) -> str:
    """
    Apply one already-computed web-verify verdict to a record:
      - a field that was EMPTY and the search found a value for -> applied
        directly (see this module's docstring, 12 Aug 2026) — nothing to
        adjudicate, no review row.
      - a field with a real, non-empty value being CONTRADICTED -> staged
        as a field_update review, same S-3b stewardship contract as every
        other pipeline path (never applied directly).
      - a clean check, or identity unconfirmed -> 'verified' / 'flagged' as
        before.

    Factored out of web_verify_pending's loop body (27 Jul) so a verdict
    already computed elsewhere — e.g. an ad-hoc verification pass run
    outside the normal no_source_excerpt-only backlog — can be staged
    through the exact same, tested path instead of duplicating this logic.

    `run_id`: the batch's real run id, threaded into the staged review for
    Phase Q2's batch-tagging (GET /reviews?run_id=...) — was always passed
    None here before 29 Jul despite DuplicateReview.run_id existing as a
    column since Phase S-3b; nothing had ever actually populated it.

    Returns the outcome key: "unchanged" | "verified" | "auto_filled" | "staged".
    "auto_filled" means every proposed field was an empty-field fill (or a
    text-field informativeness upgrade — see this module's docstring) and
    nothing needed a review; "staged" means at least one real conflict is
    now pending review (fills, if any, were still applied alongside it).
    """
    from processing.storage import _create_review, _has_conflicting_pending_fill
    from processing.field_policy import split_proposal

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

    # Text fields already went through field_policy.better_freetext inside
    # build_proposal, so a surviving text proposal IS the deterministic
    # winner — apply it unconditionally, same as storage._diff_fields does.
    # No multi-candidate guard needed here: the informativeness comparison
    # is always "record's current value vs. this one finding", not "which
    # of several empty-field candidates wins" — see field_policy.py.
    text_winners = {a: p for a, p in proposed.items() if a in _TEXT_FIELDS}
    non_text = {a: p for a, p in proposed.items() if a not in _TEXT_FIELDS}

    fills, conflicts = split_proposal(non_text)

    applied = {}
    for attr, p in {**text_winners, **fills}.items():
        # Multi-candidate safety guard (Phase Z, 12 Aug) for non-text fills —
        # see processing.storage._has_conflicting_pending_fill's docstring
        # for the incident (130 fields clobbered across 92 startups) this
        # prevents. Text winners bypass it per the comment above.
        if attr not in text_winners and _has_conflicting_pending_fill(db, record.id, attr, p["new"]):
            conflicts[attr] = p
            continue
        setattr(record, attr, p["new"])
        applied[attr] = {"value": p["new"], "source_url": p["source_url"],
                         "applied_at": p["incoming_extracted_at"]}

    if conflicts:
        _create_review(
            db,
            review_type="field_update",
            master=record,
            incoming_row=None,
            incoming_data={"name": record.name},
            proposed_changes=conflicts,
            evidence={"web_verdict": verdict, "search_results": results},
            risk_level="high",
            confidence=None,
            source="web_verification",
            run_id=run_id,
        )
        record.verification_notes = verdict.get("summary") or ""
        record.verification_evidence = {
            "reason": "web_verification_flagged", "web_verdict": verdict,
            "search_results": results, "auto_filled": applied,
        }
        record.verified_at = datetime.utcnow()
        db.commit()
        return "staged"

    if applied:
        record.verification_status = "verified"
        record.verification_notes = (
            f"Auto-filled from web search (no existing value to conflict with): "
            f"{', '.join(applied)}."
        )
        record.verification_evidence = {
            "reason": "web_verified", "web_verdict": verdict,
            "search_results": results, "auto_filled": applied,
        }
        record.verified_at = datetime.utcnow()
        db.commit()
        return "auto_filled"

    record.verification_status = "verified"
    record.verification_notes = verdict.get("summary") or "Confirmed via web search."
    record.verification_evidence = {
        "reason": "web_verified", "web_verdict": verdict,
        "search_results": results,
    }
    record.verified_at = datetime.utcnow()
    db.commit()
    return "verified"


async def web_verify_pending(limit: int = 15, run_id=None, progress=None) -> dict:
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
    counts = {"verified": 0, "auto_filled": 0, "staged": 0, "unchanged": 0, "errors": 0}
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
        return await _process_candidates(db, candidates, run_id, progress)
    finally:
        db.close()


async def _process_candidates(db, candidates: list, run_id, progress) -> dict:
    """
    The shared per-record loop: search -> verdict -> apply_verdict, with the
    consecutive-timeout circuit breaker and progress bookkeeping.

    Factored out (Phase X-4) so web_verify_pending (the no-excerpt backlog),
    web_verify_ids (a human's explicit selection) and web_verify_new_stubs
    (freshly-ingested name-only records) share ONE implementation and can
    never drift apart in their error handling. Callers differ only in which
    rows they select — that difference is the whole point, and it stays in
    the callers.
    """
    counts = {"verified": 0, "auto_filled": 0, "staged": 0, "unchanged": 0, "errors": 0}
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

            outcome = apply_verdict(db, record, results, verdict, run_id=run_id)
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


async def web_verify_new_stubs(limit: int = 25, run_id=None, progress=None) -> dict:
    """
    Enrich freshly-ingested NAME-ONLY records (Phase X-4, 5 Aug).

    The adaptive pipeline's "if the page only gives you names, take the names"
    fallback produces records with a name and nothing else — a logo grid is
    working as designed when it yields 37 bare names. This is the other half
    of that contract: those stubs go straight to web search for their real
    details, rather than sitting bare until someone notices.

    Chained after every web ingestion run (scout_controller.
    run_web_source_then_verify) so new records are enriched in the same
    session, and newest-first so a chained run naturally picks up exactly the
    stubs the run that just finished created.

    THE SELECTION PREDICATE MUST BE SELF-CLEARING — this is the one thing to
    preserve if you touch this query. "description IS NULL AND website IS
    NULL" alone is NOT: apply_verdict stages a review rather than writing
    fields, so the row stays a bare stub forever, and the same top-N records
    would be re-searched on every single run while the tail is never reached
    — burning outbound search calls and enriching nothing. That exact bug
    hit the sibling query live on 24 Jul (see web_verify_pending's comment).
    The guard here is the evidence reason: apply_verdict writes exactly one
    of web_verified / web_verification_flagged / identity_unconfirmed on
    every terminal branch, so excluding those three means "never web-verified
    before" and the predicate genuinely stops matching once processed.

    Returns the same {"verified","staged","unchanged","errors"} shape.
    Never raises.
    """
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    try:
        reason = cast(Startup.verification_evidence, JSONB)["reason"].astext
        candidates = (
            db.query(Startup)
            .filter(Startup.description.is_(None) | (Startup.description == ""))
            .filter(Startup.website.is_(None) | (Startup.website == ""))
            .filter(
                Startup.verification_evidence.is_(None)
                | reason.is_(None)
                | ~reason.in_(_WEB_VERIFIED_REASONS)
            )
            .order_by(Startup.created_at.desc().nullslast())
            .limit(limit)
            .all()
        )
        logger.info(f"[WebVerifier] New-stub batch: {len(candidates)} candidate(s)")
        return await _process_candidates(db, candidates, run_id, progress)
    finally:
        db.close()


async def web_verify_ids(ids: list, run_id=None, progress=None) -> dict:
    """
    Phase Q2 (29 Jul): web-verify an explicit, human-selected set of
    startups from Browse's bulk-selection toolbar — NOT restricted to the
    no_source_excerpt backlog (web_verify_pending's SQL filter): a human
    explicitly picking a record should be checkable regardless of whether
    it already has a source_excerpt or is already verified. Same
    search -> verify -> apply_verdict sequence, same consecutive-timeout
    circuit breaker, `run_id` threaded through for batch tagging.

    Returns {"verified": n, "staged": n, "unchanged": n, "errors": n}.
    Never raises. If Ollama fails partway through, remaining records in
    this batch are left as-is (retried on the next explicit run).
    """
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    counts = {"verified": 0, "auto_filled": 0, "staged": 0, "unchanged": 0, "errors": 0}
    try:
        records = db.query(Startup).filter(Startup.id.in_(ids)).all()
        if progress is not None:
            progress.total = len(records)

        loop = asyncio.get_event_loop()
        ollama_down = False
        consecutive_timeouts = 0
        CONSECUTIVE_TIMEOUT_THRESHOLD = 3

        for record in records:
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

                outcome = apply_verdict(db, record, results, verdict, run_id=run_id)
                counts[outcome] += 1

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[WebVerifier] Selected web-verify failed for {record.id} ({record.name}): {exc}"
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

        logger.info(f"[WebVerifier] Selected batch: {counts}")
        return counts
    finally:
        db.close()
