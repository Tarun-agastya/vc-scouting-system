"""
Centralised write path: PostgreSQL first, then Qdrant.

Data-stewardship model (Phase S-3b): the pipeline NEVER auto-merges records
or auto-overwrites existing startup data. Given an incoming extraction it:
  - inserts it as a new master if nothing resembles it (`new_master`);
  - recognizes an exact same-record fingerprint and, if a field changed,
    STAGES the change for human review without touching the master
    (`staged_update`) — or does nothing if unchanged (`no_op`);
  - for a resembling-but-not-exact record, inserts it as its own master
    (never lost) and STAGES the possible-duplicate/anomaly pair
    (`staged_duplicate` / `staged_anomaly`).
Provenance (source_history) and derived scores are the only things applied to
an existing master automatically — never extracted startup data.
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
) -> tuple:
    """
    Write path with staged review (Phase S-3b). See module docstring.

    provenance : optional dict with any of "source_name", "sender", "subject"
      (newsletter attribution). Web/RSS callers don't need it — source_name is
      resolved from the source registry by source_url.

    Returns (record_id, status):
      record_id — stable UUID string of the master this touched, or None on skip/error
      status    — one of: "new_master" | "no_op" | "staged_update"
                  | "staged_duplicate" | "staged_anomaly"
    """
    from database.connection import SessionLocal
    from database.models import Startup
    from embeddings.embedder import embedder
    from vector_db.qdrant_store import qdrant_store
    from sqlalchemy.orm.attributes import flag_modified
    from processing.matcher import build_match_report

    name = (startup.get("name") or "").strip()
    if not name or len(name) < 2:
        return None, None

    website = startup.get("website") or ""
    domain = extract_domain(website)
    # A fingerprint is only reliable identity with a real domain. No-website
    # records store NULL (repeated NULLs are allowed under UNIQUE) and rely on
    # the multi-signal matcher — so two same-named no-website companies coexist.
    fingerprint = generate_fingerprint(name, website) if domain else None
    stable_id = name_to_stable_uuid(name, website)
    if not stable_id:
        return None, None

    now = datetime.utcnow()
    source_name = (provenance or {}).get("source_name") or _resolve_source_name(source_url)
    source_entry = {
        "source": source,
        "source_name": source_name,
        "url": source_url,
        "date": published_date or now.isoformat(),
        "extracted_at": now.isoformat(),
        "run_id": _get_current_run_id(),
    }
    if provenance:
        if provenance.get("sender"):
            source_entry["sender"] = provenance["sender"]
        if provenance.get("subject"):
            source_entry["subject"] = provenance["subject"]

    db = SessionLocal()
    try:
        # Embed once — reused for matcher blocking and (for new masters) Qdrant sync.
        incoming_vector = embedder.embed(embedder.build_startup_text(startup))

        report = build_match_report(startup, db, incoming_vector)
        logger.info(f"[Storage] Match '{name}': {report.outcome} "
                    f"(conf={report.confidence}, {report.reason})")

        # ── Exact same record → no_op or staged field-update (never overwrite) ─
        if report.outcome == "exact_same_record":
            master = db.query(Startup).filter(Startup.id == report.master_id).first()
            if master is not None:
                proposed, risk = _diff_fields(master, startup, source, now.isoformat(), db)
                _append_source_history(master, source_entry, flag_modified)
                _rescore(master, source_url, db, flag_modified)
                _backfill_source_excerpt(master, startup, flag_modified)
                if proposed:
                    _create_review(
                        db, review_type="field_update", master=master, incoming_row=None,
                        incoming_data=startup, proposed_changes=proposed, evidence=report.evidence,
                        risk_level=risk, confidence=report.confidence, source=source,
                        run_id=source_entry["run_id"],
                    )
                    db.commit()
                    return str(master.id), "staged_update"
                db.commit()
                return str(master.id), "no_op"
            # master vanished — fall through to insert as new

        # ── Insert the incoming as its own master (new / duplicate / anomaly) ──
        new_row = _insert_master(
            db, startup, name, website, fingerprint, stable_id,
            source, source_url, source_entry, published_date, now,
        )
        _score_and_index(new_row, startup, incoming_vector, fingerprint, source,
                         source_url, published_date, db, qdrant_store, flag_modified)

        if report.outcome in ("possible_duplicate", "anomaly"):
            if _is_known_different(db, report.master_id, str(new_row.id)):
                db.commit()
                return str(new_row.id), "new_master"  # human already said "different"
            master = db.query(Startup).filter(Startup.id == report.master_id).first()
            rtype = "anomaly" if report.outcome == "anomaly" else "possible_duplicate"
            _create_review(
                db, review_type=rtype, master=master, incoming_row=new_row,
                incoming_data=startup, proposed_changes=None, evidence=report.evidence,
                risk_level=report.risk_level, confidence=report.confidence, source=source,
                run_id=source_entry["run_id"],
            )
            db.commit()
            return str(new_row.id), ("staged_anomaly" if rtype == "anomaly" else "staged_duplicate")

        db.commit()
        return str(new_row.id), "new_master"

    except Exception as exc:
        logger.error(f"[Storage] Failed to store '{name}': {exc}")
        db.rollback()
        return None, None
    finally:
        db.close()


# ── Insert / score / index a new master ───────────────────────────────────────

def _insert_master(db, startup, name, website, fingerprint, stable_id,
                   source, source_url, source_entry, published_date, now):
    """Create + commit a new Startup master (with id-collision handling)."""
    from database.models import Startup
    contact_raw = startup.get("contact_info") or ""
    linkedin_val = contact_raw if "linkedin.com" in contact_raw else None

    # Phase H-1 internal keys (set by qwen_client.extract_startups): pull
    # them out for the verification columns, then strip so they don't
    # pollute raw_data or (via the **startup spread in _score_and_index)
    # the Qdrant payload. Absent entirely for non-pipeline inserts (manual
    # /add-startup) — .pop(..., None) handles that fine.
    source_excerpt = startup.pop("_source_excerpt", None)
    grounding_note = startup.pop("_grounding", None)
    verification_evidence = {"h1_grounding": grounding_note} if grounding_note else None

    # stable_id is name-derived; two different same-named no-website companies
    # would collide on it — mint a fresh id if it's already taken. As of
    # Phase D-1, a same-name no-website RE-SIGHTING is caught upstream in
    # matcher.py (exact_same_record) before ever reaching this insert path —
    # so this branch firing now means a genuinely different company shares
    # this exact normalized name (or the matcher was bypassed some other
    # way). Logged so that case stays visible instead of silently minting a
    # new id, per the plan's disclosed-risk decision.
    insert_id = stable_id
    if db.query(Startup).filter(Startup.id == stable_id).first() is not None:
        import uuid as _uuid
        insert_id = str(_uuid.uuid4())
        logger.warning(
            f"[Storage] stable_id collision for '{name}' (id={stable_id}) — "
            f"minting a new id ({insert_id}) instead. Should be rare after "
            f"Phase D-1; investigate if this fires often."
        )

    row = Startup(
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
        extracted_at=now,
        raw_data=startup,
        verification_status="unverified",
        verification_evidence=verification_evidence,
        source_excerpt=source_excerpt,
    )
    db.add(row)
    db.commit()
    logger.debug(f"[Storage] Inserted new master: {name}")
    return row


def _score_and_index(row, startup, vector, fingerprint, source, source_url,
                     published_date, db, qdrant_store, flag_modified):
    """Compute the deterministic score and upsert the master's vector to Qdrant."""
    from processing.scorer import compute_enrichment_score
    score_result = compute_enrichment_score(row)
    row.enrichment_score  = score_result.enrichment_score
    row.source_confidence = score_result.source_confidence
    row.score_tier        = score_result.score_tier
    row.score_breakdown   = score_result.score_breakdown
    row.last_enriched_at  = datetime.utcnow()
    flag_modified(row, "score_breakdown")
    db.commit()

    qdrant_store.upsert_startup(str(row.id), vector, {
        **startup,
        "id": str(row.id),
        "fingerprint": fingerprint,
        "source": source,
        "source_url": source_url,
        "published_date": published_date,
        "extracted_at": row.extracted_at.isoformat() if row.extracted_at else None,
        "enrichment_score": row.enrichment_score or 0.0,
        "source_confidence": row.source_confidence or 0.0,
        "score_tier": row.score_tier or "WEAK_SIGNAL",
        "verification_status": row.verification_status or "unverified",
    })


# ── Provenance & scoring on an EXISTING master (auto — not startup data) ───────

def _append_source_history(master, source_entry, flag_modified) -> None:
    """Append this sighting to the master's source_history (audit log, not data)."""
    history = list(master.source_history or [])
    if source_entry.get("url") not in {e.get("url") for e in history}:
        history.append(source_entry)
        master.source_history = history
        flag_modified(master, "source_history")


def _rescore(master, source_url, db, flag_modified) -> None:
    """Refresh the master's derived score if a new source warrants it."""
    try:
        from processing.scorer import compute_enrichment_score, should_rescore
        if not should_rescore(master, source_url):
            return
        r = compute_enrichment_score(master)
        master.enrichment_score  = r.enrichment_score
        master.source_confidence = r.source_confidence
        master.score_tier        = r.score_tier
        master.score_breakdown   = r.score_breakdown
        master.last_enriched_at  = datetime.utcnow()
        flag_modified(master, "score_breakdown")
    except Exception as exc:
        logger.warning(f"[Storage] rescore skipped: {exc}")


def _backfill_source_excerpt(master, startup: dict, flag_modified) -> None:
    """
    Backfill Phase H-1's source_excerpt onto an existing master that doesn't
    have one yet (23 Jul follow-up to Phase D-1).

    Without this, a record flagged verification_status="flagged" with reason
    "no_source_excerpt" (everything ingested before H-1 shipped) could never
    actually be fixed by re-ingestion as the review-inbox notes promised: the
    exact_same_record path only ever called _append_source_history/_rescore,
    so a freshly-captured excerpt sitting right there in `startup` was read
    and then silently discarded on every no_op/staged_update outcome — the
    record stayed permanently unverifiable no matter how many times its
    source was re-crawled. This doesn't verify anything itself (that's still
    H-3's job) — it just stops throwing away the one thing H-3 needs to ever
    be able to.

    Never overwrites an existing excerpt (first-writer-wins is fine — the
    goal is only to escape the "nothing on file" trap, not to keep the
    excerpt fresh on every re-sighting).

    Also re-queues the record for a real check: processing/verifier.py's
    recheck only ever pulls verification_status="unverified" rows, so a
    record already sitting "flagged" (specifically for reason
    no_source_excerpt — see verifier.py's Phase A) would otherwise never be
    picked up again even with an excerpt now on file. Only resets status for
    THAT specific reason — a record flagged for an actual Layer-2 finding
    (a real contradiction) is left alone; that still needs a human, not an
    auto-reset.
    """
    if master.source_excerpt:
        return
    excerpt = startup.get("_source_excerpt")
    if not excerpt:
        return
    master.source_excerpt = excerpt
    grounding_note = startup.get("_grounding")

    was_no_source_flag = (
        master.verification_status == "flagged"
        and (master.verification_evidence or {}).get("reason") == "no_source_excerpt"
    )
    if was_no_source_flag:
        # The ONLY reason it was flagged no longer applies — re-queue it for
        # a real check instead of leaving it stuck "flagged" forever. Clear
        # the stale "no_source_excerpt" marker (replaced by the fresh
        # grounding note if H-1 found anything, else just cleared).
        master.verification_status = "unverified"
        master.verification_notes = None
        master.verified_at = None
        master.verification_evidence = {"h1_grounding": grounding_note} if grounding_note else None
        flag_modified(master, "verification_evidence")
    elif grounding_note and not master.verification_evidence:
        master.verification_evidence = {"h1_grounding": grounding_note}
        flag_modified(master, "verification_evidence")

    logger.info(
        f"[Storage] Backfilled source_excerpt onto existing master "
        f"'{master.name}' ({master.id}) from a re-sighting"
        + (" — re-queued for recheck (was flagged only for missing excerpt)" if was_no_source_flag else "")
    )


# ── Diff (meaningful change detection) ────────────────────────────────────────

# model attribute → incoming dict key
_DIFF_FIELDS = {
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
}
# Free-text fields where trivial rewording should NOT count as a change.
_TEXT_FIELDS = {"short_description", "description"}


def _norm(v) -> str:
    return (str(v).strip().lower()) if v is not None else ""


def _text_changed(old: str, new: str) -> bool:
    """True only if two texts differ substantially (ignores minor rewording)."""
    if not old or not new:
        return bool(new) and not old  # blank→value counts; value→blank ignored
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(old.lower(), new.lower()) < 90
    except ImportError:
        return _norm(old) != _norm(new)


def _diff_fields(master, incoming: dict, source: str, extracted_at_iso: str, db) -> tuple:
    """
    Compute the meaningful diff between an existing master and an incoming
    extraction. Returns (proposed_changes, risk_level).
      proposed_changes: {field: {old, new, incoming_source, incoming_extracted_at}}
      risk_level: "high" if any populated field would change, else "low".
    Suppressed (previously human-rejected) values are dropped.
    """
    proposed = {}
    high_risk = False

    for attr, key in _DIFF_FIELDS.items():
        new_val = incoming.get(key)
        if new_val is None or str(new_val).strip() == "":
            continue  # no new info
        old_val = getattr(master, attr, None)

        if attr in _TEXT_FIELDS:
            changed = _text_changed(_norm(old_val), str(new_val))
        else:
            changed = _norm(old_val) != _norm(new_val)
        if not changed:
            continue
        if _is_value_suppressed(db, master.id, attr, str(new_val)):
            continue

        proposed[attr] = {
            "old": old_val, "new": new_val,
            "incoming_source": source, "incoming_extracted_at": extracted_at_iso,
        }
        if old_val not in (None, "", []):
            high_risk = True

    # founders / tags — additive enrichment (new names not already present)
    inc_founders = [f for f in (incoming.get("founders") or []) if isinstance(f, str) and f.strip()]
    cur_founders = list((master.raw_data or {}).get("founders") or [])
    new_founders = [f for f in inc_founders if f.strip().lower() not in {c.strip().lower() for c in cur_founders}]
    if new_founders:
        proposed["founders"] = {
            "old": cur_founders, "new": cur_founders + new_founders,
            "incoming_source": source, "incoming_extracted_at": extracted_at_iso,
        }

    inc_tags = [t for t in (incoming.get("tags") or []) if isinstance(t, str) and t.strip()]
    cur_tags = list(master.tags or [])
    new_tags = [t for t in inc_tags if t.strip().lower() not in {c.strip().lower() for c in cur_tags}]
    if new_tags:
        proposed["tags"] = {
            "old": cur_tags, "new": cur_tags + new_tags,
            "incoming_source": source, "incoming_extracted_at": extracted_at_iso,
        }

    return proposed, ("high" if high_risk else "low")


# ── Suppression (human-reject memory) ─────────────────────────────────────────

def _is_known_different(db, master_id, other_id) -> bool:
    from database.models import SuppressedMatch
    if not master_id or not other_id:
        return False
    return db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "known_different",
        SuppressedMatch.master_id.in_([master_id, other_id]),
        SuppressedMatch.other_id.in_([master_id, other_id]),
    ).first() is not None


def _is_value_suppressed(db, master_id, field, value) -> bool:
    from database.models import SuppressedMatch
    return db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "rejected_value",
        SuppressedMatch.master_id == master_id,
        SuppressedMatch.field == field,
        SuppressedMatch.value == value,
    ).first() is not None


# ── Review creation ───────────────────────────────────────────────────────────

def _has_equivalent_pending_review(db, *, review_type, master_id, proposed_changes, incoming_name) -> bool:
    """
    True if a pending review already covers this exact issue for this master
    (Phase D-1, 22 Jul). Without this, re-running the same source N times
    stages N identical review items for a human to triage — this caps it at
    one open review per real issue no matter how many times ingestion re-runs.

    field_update           -> an existing pending field_update whose proposed
                              {field: new_value} mapping is identical.
    possible_duplicate/anomaly -> an existing pending review of the same type
                              whose incoming_name normalizes the same.
    """
    from database.models import DuplicateReview

    q = db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == review_type,
        DuplicateReview.status == "pending",
    )
    if review_type == "field_update":
        new_changes = {f: c.get("new") for f, c in (proposed_changes or {}).items()}
        return any(
            {f: c.get("new") for f, c in (existing.proposed_changes or {}).items()} == new_changes
            for existing in q.all()
        )
    norm_name = normalize_company_name(incoming_name or "")
    return any(
        normalize_company_name(existing.incoming_name or "") == norm_name
        for existing in q.all()
    )


def _create_review(db, *, review_type, master, incoming_row, incoming_data,
                   proposed_changes, evidence, risk_level, confidence, source, run_id) -> None:
    """
    Create a staged review row. Never raises (a review failure must not fail
    the upsert). Idempotent (Phase D-1): skips if an equivalent pending
    review already exists for this master, so re-ingestion never stacks
    duplicate review items.
    """
    try:
        from database.models import DuplicateReview

        if master is not None and _has_equivalent_pending_review(
            db, review_type=review_type, master_id=master.id,
            proposed_changes=proposed_changes,
            incoming_name=(incoming_data.get("name") or "").strip(),
        ):
            logger.debug(
                f"[Storage] Skipping duplicate {review_type} review for "
                f"'{master.name}' — an equivalent pending review already exists"
            )
            return

        review = DuplicateReview(
            review_type=review_type,
            master_id=master.id if master is not None else None,
            master_name=master.name if master is not None else None,
            incoming_id=incoming_row.id if incoming_row is not None else None,
            incoming_name=(incoming_data.get("name") or "").strip(),
            incoming_data=incoming_data,
            proposed_changes=proposed_changes,
            evidence=evidence,
            risk_level=risk_level,
            confidence=confidence,
            source=source,
            run_id=run_id,
            status="pending",
        )
        db.add(review)
        db.flush()
        logger.info(f"[Storage] Staged {review_type} review "
                    f"('{review.incoming_name}' ~ '{review.master_name}', risk={risk_level})")
    except Exception as exc:
        logger.error(f"[Storage] Failed to record review: {exc}")


# ── Misc helpers ──────────────────────────────────────────────────────────────

def _fill_empty_fields(existing, new_data: dict) -> None:
    """
    Copy fields from new_data into an existing Startup row only when the
    current value is None or empty. Never overwrites populated data.
    Used by the reviews approve-path and scripts/dedup_sweep.py.
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

    if not existing.founded_year and new_data.get("founded_year"):
        existing.founded_year = _safe_int(new_data["founded_year"])

    if new_data.get("tags"):
        current_tags = set(existing.tags or [])
        new_tags = set(new_data["tags"])
        if new_tags - current_tags:
            existing.tags = list(current_tags | new_tags)


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except Exception:
        return None
