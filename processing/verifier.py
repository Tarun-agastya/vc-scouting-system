"""
Recheck pass for stored startups (Phase H-3) — the "bulletproof" layer.

Two checks run per unverified record that has a source_excerpt:
  Layer 1 (deterministic re-ground, free, no GPU): re-runs the exact H-1
    grounding rules (reasoning.qwen_client._ground_startup) against the
    record's own source_excerpt and nulls any fabrication-prone field still
    unsupported. Applied directly — no human review needed, consistent with
    H-1's own behavior (a blank field is always safe; that's the whole
    "correct-and-less over wrong-and-more" principle).
  Layer 2 (14B deep-recheck, under the GPU mutex): the local reasoning
    model re-reads source_excerpt alongside EVERY structured field on the
    record — not just the H-1-gated ones — and reports whether the excerpt
    even describes this company, plus which fields it contradicts or simply
    doesn't mention. This is the general-purpose net: it's the only layer
    that can catch a wrong industry, a fabricated description, or a
    misidentified company, none of which Layer 1's literal-token grounding
    can see. It only classifies — it never proposes a replacement value and
    never changes verification_status by itself beyond flag/verified.

A record with NO source_excerpt (everything ingested before Phase H-1
shipped, 21 Jul 2026) can't be checked against anything. Rather than being
silently stuck at "unverified" forever, or guessed at as "verified", it's
resolved immediately (no GPU needed) to "flagged" with a distinct
"no_source_excerpt" reason — this makes the true, measured state of the
backlog visible instead of assumed, and it's exactly the honest signal the
owner asked for: don't hide what can't be checked.

A "flagged" record is never auto-corrected by Layer 2 findings — it always
waits for a human, consistent with the Phase S-3b stewardship model. Layer 2
never proposes a replacement value for any field. The one exception (added
27 Jul after a live find — see the "layer2_nulled" logic below) is nulling,
never guessing: if Layer 2 says one of the same high-risk fields Layer 1
already gates (founded_year, funding_stage, employee_count) has no support
in the record's own source_excerpt, it's cleared the same way Layer 1
clears an ungrounded field — an absence, not a proposed correction.

Called exclusively through processing.scout_controller.run_recheck(), which
wraps the whole batch in the GPU mutex via ScoutController._execute() — do
NOT acquire scout_controller.gpu_mutex again inside this module (unlike
processing/review_explainer.py, which is a bare scheduled job and acquires
it per-item itself). asyncio.Lock is not reentrant; re-acquiring a lock the
caller already holds would deadlock the whole recheck run.
"""
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_NO_SOURCE_NOTE = (
    "No source excerpt on file — this record predates the Phase H-1 "
    "grounding system (21 Jul 2026) and can't be automatically re-verified "
    "against its original source text. Needs manual review or re-ingestion."
)

# Fields Layer 2 is shown and asked to judge. Deliberately broader than
# H-1's 5 deterministically-gated fields (founded_year, funding_stage,
# funding_amount, employee_count, founders) — this is what makes the recheck
# general rather than a repeat of H-1's own checks (plan addendum 3, scope
# correction 21 Jul 2026).
_RECHECK_FIELDS = [
    "name", "short_description", "description", "industry", "sub_industry",
    "tech_cluster", "country", "city", "address", "funding_stage",
    "founded_year", "employee_count", "contact_info", "website",
]


def _layer1_reground(record) -> dict:
    """
    Re-run the H-1 deterministic grounding rules against source_excerpt.
    Returns {field: new_value} for anything it nulled/changed. Empty if the
    grounding gate is disabled in config, or nothing changed.
    """
    from reasoning.qwen_client import _ground_startup
    from config.tuning_loader import get_grounding_config

    cfg = get_grounding_config()
    if not cfg.get("enabled", True):
        return {}

    raw = record.raw_data or {}
    snapshot = {
        "founded_year": record.founded_year,
        "funding_stage": record.funding_stage,
        "funding_amount": raw.get("funding_amount"),
        "employee_count": record.employee_count,
        "founders": raw.get("founders") or [],
    }
    grounded = _ground_startup(dict(snapshot), record.source_excerpt, cfg)

    changes = {}
    for field in ("founded_year", "funding_stage", "funding_amount", "employee_count"):
        if grounded.get(field) != snapshot[field]:
            changes[field] = grounded[field]
    if grounded.get("founders") != snapshot["founders"]:
        changes["founders"] = grounded["founders"]
    return changes


def _build_recheck_prompt(record) -> str:
    from reasoning.prompts import VERIFICATION_RECHECK_PROMPT

    field_lines = "\n".join(
        f"  - {f}: {getattr(record, f, None)!r}" for f in _RECHECK_FIELDS
    )
    return VERIFICATION_RECHECK_PROMPT.format(
        source_excerpt=(record.source_excerpt or "")[:2000],
        name=record.name,
        fields=field_lines,
    )


def _layer2_deep_recheck(record) -> dict:
    """Synchronous — dispatched via run_in_executor by the caller."""
    from reasoning.qwen_client import qwen_client

    prompt = _build_recheck_prompt(record)
    return qwen_client.recheck_record(prompt)


async def _recheck_one(db, record) -> str:
    """
    Layer 1 + Layer 2 recheck for ONE record — the reusable unit both
    recheck_pending (backlog drain) and recheck_ids (explicit human
    selection, Phase Q2, 29 Jul) share. Raises on failure — each caller's
    own loop handles retry/circuit-breaker bookkeeping (same split as
    processing/web_verifier.py's apply_verdict/build_proposal, 27 Jul: one
    place decides the outcome, callers decide how to iterate).

    Returns "verified" or "flagged". Commits internally (via db.commit()
    or _reindex, which commits internally too).
    """
    from api.routes.reviews import _reindex

    loop = asyncio.get_event_loop()

    layer1_changes = _layer1_reground(record)
    if layer1_changes:
        from processing.field_policy import safe_string_list

        raw = dict(record.raw_data or {})
        for field, value in layer1_changes.items():
            if field == "founders":
                # Guarded like every other founders/tags write site — see
                # safe_string_list's docstring. _ground_startup is fixed at
                # source now, but this is the last hop before the value is
                # persisted, so it must not be the one place that trusts
                # whatever it was handed.
                raw[field] = safe_string_list(value)
            elif field in ("funding_amount",):
                # Not a Startup column — lives only in raw_data (same as the
                # extraction pipeline stores it), so a plain setattr() here
                # would silently create an unmapped, never-persisted
                # attribute and the grounding gate's nulling would appear to
                # work but never actually reach the database.
                raw[field] = value
            else:
                setattr(record, field, value)
        record.raw_data = raw

    verdict = await loop.run_in_executor(None, _layer2_deep_recheck, record)

    contradicted = verdict.get("contradicted_fields") or []
    unsupported = set(verdict.get("unsupported_fields") or [])
    identity_ok = verdict.get("identity_match", True)
    # Flag on a real contradiction or a misidentified company — NOT merely
    # on "unsupported" fields, which just means this particular excerpt
    # doesn't mention them (they may still be correct from an earlier
    # sighting). See plan addendum 3.
    is_flagged = (not identity_ok) or bool(contradicted)

    # BUT: for the same high-fabrication-risk fields H-1 already gates at
    # ingest time, "unsupported" here means Layer 2 — reading the record's
    # OWN source_excerpt — found no support for a value still sitting on
    # the record. Null it here too, mirroring Layer 1's own discipline.
    # Never touches paraphrased fields (industry/city/description).
    layer2_nulled = []
    for field in ("founded_year", "funding_stage", "employee_count"):
        if field in unsupported and getattr(record, field, None) is not None:
            setattr(record, field, None)
            layer2_nulled.append(field)
    if "funding_amount" in unsupported and (record.raw_data or {}).get("funding_amount") is not None:
        raw = dict(record.raw_data or {})
        raw["funding_amount"] = None
        record.raw_data = raw
        layer2_nulled.append("funding_amount")

    record.verification_status = "flagged" if is_flagged else "verified"
    record.verification_notes = verdict.get("summary") or ""
    record.verification_evidence = {
        "layer1_reground": layer1_changes or None,
        "layer2": verdict,
        "layer2_nulled": layer2_nulled or None,
    }
    record.verified_at = datetime.utcnow()

    if layer1_changes or layer2_nulled:
        _reindex(db, record)  # re-score/re-embed — commits internally
    else:
        db.commit()

    return "flagged" if is_flagged else "verified"


async def recheck_pending(limit: int = 20, progress=None) -> dict:
    """
    Process the unverified backlog. Two phases:

      Phase A — instantly resolve EVERY unverified record with no
      source_excerpt (nothing to check against; free, no GPU, unbounded —
      there's no reason to cap something that costs nothing).

      Phase B — full Layer 1 + Layer 2 recheck for up to `limit` records
      that DO have a source_excerpt (GPU-bound via Layer 2, one at a time).

    Returns {"verified": n, "flagged": n, "no_source": n, "errors": n}.
    Never raises. If Ollama fails partway through Phase B, the remaining
    records in this batch are left "unverified" (retried on the next run)
    rather than guessed at.

    `progress`: optional scout_controller.RecordProgress — updated per Phase
    B record so /ingestion/status shows real "processed / total" counters
    while running (Phase A is near-instant and free, not worth tracking).
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from api.routes.reviews import _reindex

    db = SessionLocal()
    counts = {"verified": 0, "flagged": 0, "no_source": 0, "errors": 0}
    try:
        # ── Phase A: no source_excerpt → flagged immediately, no GPU ──────────
        no_source_rows = (
            db.query(Startup)
            .filter(Startup.verification_status == "unverified")
            .filter((Startup.source_excerpt.is_(None)) | (Startup.source_excerpt == ""))
            .all()
        )
        for row in no_source_rows:
            row.verification_status = "flagged"
            row.verification_notes = _NO_SOURCE_NOTE
            row.verification_evidence = {"reason": "no_source_excerpt"}
            row.verified_at = datetime.utcnow()
        if no_source_rows:
            db.commit()
        counts["no_source"] = len(no_source_rows)
        counts["flagged"] += len(no_source_rows)

        # ── Phase B: full recheck for records with a source_excerpt ───────────
        checkable = (
            db.query(Startup)
            .filter(Startup.verification_status == "unverified")
            .filter(Startup.source_excerpt.isnot(None))
            .filter(Startup.source_excerpt != "")
            .order_by(Startup.created_at.asc())
            .limit(limit)
            .all()
        )
        if progress is not None:
            progress.total = len(checkable)

        ollama_down = False
        consecutive_timeouts = 0
        # Require several timeouts IN A ROW before concluding Ollama itself
        # is down, not just one slow record. Found live 27 Jul: a single
        # record landing on the slow tail of the latency range (30-265s
        # observed this session) tripped this on ONE timeout, and every
        # night for 5+ days the scheduled 03:00 recheck hit an early record,
        # timed out once, and silently abandoned the entire rest of a
        # 400+ record batch without even attempting the others — the real
        # reason the backlog never drained. recheck_record now also retries
        # once internally, so this counter should rarely even move.
        CONSECUTIVE_TIMEOUT_THRESHOLD = 3

        for record in checkable:
            if progress is not None:
                progress.current_name = record.name
            try:
                if ollama_down:
                    # Already know it's unreachable — don't hammer it for
                    # the rest of the batch; leave "unverified" for retry.
                    continue

                outcome = await _recheck_one(db, record)
                counts[outcome] += 1
                consecutive_timeouts = 0

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[Verifier] Recheck failed for {record.id} ({record.name}): {exc}"
                )
                counts["errors"] += 1
                msg = str(exc).lower()
                if any(s in msg for s in ("connect", "timeout", "timed out", "refused", "unreachable")):
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= CONSECUTIVE_TIMEOUT_THRESHOLD:
                        ollama_down = True
                        logger.warning(
                            f"[Verifier] {CONSECUTIVE_TIMEOUT_THRESHOLD} consecutive timeouts — "
                            f"treating Ollama as down for the rest of this batch"
                        )
                else:
                    consecutive_timeouts = 0
            finally:
                if progress is not None:
                    progress.processed += 1

        logger.info(f"[Verifier] Recheck batch: {counts}")
        return counts
    finally:
        db.close()


async def recheck_ids(ids: list, progress=None) -> dict:
    """
    Phase Q2 (29 Jul): recheck an explicit, human-selected set of startups
    from Browse's bulk-selection toolbar — deliberately NO verification_status
    filter (unlike recheck_pending's backlog drain), since a human explicitly
    picking a record is allowed to re-check one that's already "verified"
    on purpose. Same Layer 1 + Layer 2 logic via _recheck_one, same
    consecutive-timeout circuit breaker as the backlog pass.

    A selected record with no source_excerpt is resolved the same honest
    way Phase A of recheck_pending does (flagged/no_source_excerpt) rather
    than crashing on an empty prompt.

    Returns {"verified": n, "flagged": n, "skipped_no_excerpt": n, "errors": n}.
    Never raises. If Ollama fails partway through, remaining records in this
    batch are left as-is (their prior status) rather than guessed at.
    """
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    counts = {"verified": 0, "flagged": 0, "skipped_no_excerpt": 0, "errors": 0}
    try:
        records = db.query(Startup).filter(Startup.id.in_(ids)).all()
        if progress is not None:
            progress.total = len(records)

        ollama_down = False
        consecutive_timeouts = 0
        CONSECUTIVE_TIMEOUT_THRESHOLD = 3

        for record in records:
            if progress is not None:
                progress.current_name = record.name
            try:
                if not record.source_excerpt:
                    record.verification_status = "flagged"
                    record.verification_notes = _NO_SOURCE_NOTE
                    record.verification_evidence = {"reason": "no_source_excerpt"}
                    record.verified_at = datetime.utcnow()
                    db.commit()
                    counts["skipped_no_excerpt"] += 1
                    continue

                if ollama_down:
                    continue

                outcome = await _recheck_one(db, record)
                counts[outcome] += 1
                consecutive_timeouts = 0

            except Exception as exc:
                db.rollback()
                logger.warning(
                    f"[Verifier] Selected recheck failed for {record.id} ({record.name}): {exc}"
                )
                counts["errors"] += 1
                msg = str(exc).lower()
                if any(s in msg for s in ("connect", "timeout", "timed out", "refused", "unreachable")):
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= CONSECUTIVE_TIMEOUT_THRESHOLD:
                        ollama_down = True
                        logger.warning(
                            f"[Verifier] {CONSECUTIVE_TIMEOUT_THRESHOLD} consecutive timeouts — "
                            f"treating Ollama as down for the rest of this batch"
                        )
                else:
                    consecutive_timeouts = 0
            finally:
                if progress is not None:
                    progress.processed += 1

        logger.info(f"[Verifier] Selected recheck batch: {counts}")
        return counts
    finally:
        db.close()
