"""
The nightly job that actually closes reviews — Phase 2 of the review-inbox
autonomy plan (plans/REVIEW_INBOX_AUTONOMY_PLAN.md).

The gap this closes
--------------------
Both adjudicators (processing/field_adjudicator.py, processing/
dedup_adjudicator.py) were built, tested, and fixed twice — and until this
module, nothing ever ran them except a human typing a script name. 0 of the
267 pending duplicates on 24 Sep had ever been judged. The gap was never
intelligence; it was that nothing closed the loop.

This module is that loop, modelled directly on review_explainer.py — "one
local-model job under the GPU mutex, reading the whole pending queue" — the
established pattern for exactly this shape of nightly work.

The asymmetric-safety policy, unchanged
----------------------------------------
This only ever ACTS on the reversible direction, same as the adjudicators
themselves:
  field_update       group_may_auto_apply -> reject_review (a suppression
                     row). Never applies a winning proposal — that
                     overwrites a stored value and stays for a human.
  possible_duplicate may_auto_apply -> reject_review (known_different).
                     Never merges a same_company verdict, at any confidence
                     — merging deletes a record.

Every review judged gets its reasoning attached to `evidence` and
`llm_explanation` whether or not it closes, so a human triaging the
leftovers always sees the model's read on it.

The multi-candidate safety rule (12 Aug incident, re-stated because a
fourth automated writer is being added here):
  1. Group by (master_id, field) across ALL pending rows BEFORE judging.
  2. Re-read the master's LIVE value at judgement time, never `change["old"]`
     from a review's frozen snapshot.
  3. Never auto-apply when >1 distinct candidate exists for one (master,
     field) — adjudicate_field_group already judges every competing
     proposal in one call against the live value, which is why it is the
     required entry point rather than adjudicating reviews one at a time.

apply=False by default. The CLI (scripts/resolve_reviews.py) and the first
scheduled runs are report-only, so a human sees real output before anything
closes automatically — the same gate both adjudicators went through by hand
before this module existed, and it caught a real prompt bug (the "kept a
value its own reasoning called wrong" bug, fixed 24 Sep) that a smaller
sample had hidden.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# After this many consecutive unusable results, treat it as "the model is
# down" rather than "this one review happened to be hard to judge" and stop
# for the night rather than working through the rest of a dead-Ollama batch.
# adjudicate_field_group/adjudicate_pair both return None for a failed CALL
# and for an unusable RESPONSE — there is no way to tell those apart from
# here — so a single-failure break (review_explainer.py's simpler pattern)
# would abandon a whole run over one flaky call. This instead mirrors the
# circuit breaker web_verifier.py already uses for the identical ambiguity.
_CONSECUTIVE_FAILURE_THRESHOLD = 3


def _parse_at(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _fresh(judged_at) -> bool:
    """Judged recently enough that asking the (deterministic) model again is waste."""
    from datetime import timedelta

    from config import settings
    return bool(judged_at) and judged_at > datetime.utcnow() - timedelta(days=settings.resolver_rejudge_days)


def _proposed(review) -> dict:
    import json
    prop = review.proposed_changes or {}
    if isinstance(prop, str):
        try:
            prop = json.loads(prop)
        except Exception:
            return {}
    return prop or {}


def _emit(on_progress, payload: dict) -> None:
    """
    Hand one just-judged item to the caller's progress callback, if any.

    A callback that raises must never take the run down with it — this is
    cosmetic reporting wrapped around work that has already been committed,
    so a broken printer is not a reason to lose the night's judgements.
    """
    if on_progress is None:
        return
    try:
        on_progress(payload)
    except Exception as exc:
        logger.warning(f"[Resolver] progress callback failed: {type(exc).__name__}: {exc}")


async def _resolve_field_updates(db, limit: int, apply: bool, stats: dict,
                                  master_ids=None, on_progress=None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from database.models import DuplicateReview, Startup
    from processing.field_adjudicator import adjudicate_field_group, group_may_auto_apply
    from processing.review_actions import record_rejection, settle_field_keep
    from processing.scout_controller import scout_controller

    query = db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending",
        DuplicateReview.review_type == "field_update")
    if master_ids is not None:
        query = query.filter(DuplicateReview.master_id.in_(master_ids))
    pending = query.order_by(DuplicateReview.created_at.asc()).all()

    # Group by (master_id, field) across every pending row before deciding
    # anything — see this module's docstring. `limit` counts GROUPS, not raw
    # review rows, matching scripts/adjudicate_field_changes.py's existing
    # convention.
    #
    # tags/founders are skipped here — found live 29 Sep running this
    # against real data: a review can propose several fields at once
    # (city, tags, sub_industry, founders all in one row is common — 34 of
    # 125 pending field_update rows mix a list field with a scalar one), and
    # tags/founders carry a whole LIST as their "new" value. Feeding that
    # into adjudicate_field_group — built for a single enum-shaped
    # candidate — produced `str(the_list)` as a garbled "candidate" like
    # "['B2B SaaS', 'HR Tech', 'startup hub']" and a confused none_fit
    # verdict. These fields are Phase 1's job (field_policy.merge_list_field
    # auto-applies a lossless union at write time); anything still pending
    # here is a MIXED row drain_list_field_reviews.py's all-list-fields-only
    # check doesn't touch — a real gap, left as pending for a human rather
    # than mis-adjudicated as a fake enum choice.
    _NOT_ADJUDICABLE = {"tags", "founders"}
    groups = defaultdict(list)
    for r in pending:
        prop = _proposed(r)
        for field, change in prop.items():
            if field in _NOT_ADJUDICABLE:
                continue
            if isinstance(change, dict):
                groups[(r.master_id, field)].append((r, change))

    # Fair rotation instead of "oldest first, every night". A review the
    # resolver can never close stays at the head of an oldest-first queue, so
    # the same ones were re-judged each night and the rest never reached.
    # Never-judged groups go first, then the longest-since-judged, and a group
    # judged recently with unchanged inputs is skipped outright.
    def _group_state(key, items):
        field = key[1]
        stamps = [_parse_at(((r.evidence or {}).get("field_adjudications") or {}).get(field, {}).get("at"))
                  for r, _c in items]
        stamps = [t for t in stamps if t]
        considered = {str(c.get("new")).strip() for _r, c in items}
        prior = next((((r.evidence or {}).get("field_adjudications") or {}).get(field, {}).get("considered")
                      for r, _c in items
                      if ((r.evidence or {}).get("field_adjudications") or {}).get(field)), None)
        same_inputs = prior is not None and {str(x).strip() for x in prior} == considered
        return (max(stamps) if stamps else None), same_inputs

    ordered = []
    for key, items in groups.items():
        judged_at, same_inputs = _group_state(key, items)
        if same_inputs and _fresh(judged_at):
            continue
        ordered.append((judged_at or datetime.min, key, items))
    ordered.sort(key=lambda t: t[0])
    groups = {key: items for _t, key, items in ordered}

    loop = asyncio.get_event_loop()
    consecutive_failures = 0
    judged = 0
    total = min(len(groups), limit)
    _emit(on_progress, {"event": "phase", "phase": "field_update", "total": total})

    for (master_id, field), items in groups.items():
        if judged >= limit:
            break
        master = db.query(Startup).filter(Startup.id == master_id).first()
        if master is None:
            continue

        current = getattr(master, field, None)  # the LIVE value — never change["old"]
        candidates = [c.get("new") for _r, c in items]

        async with scout_controller.gpu_mutex:
            res = await loop.run_in_executor(
                None, adjudicate_field_group, master, field, current, candidates)

        if not res:
            consecutive_failures += 1
            stats["unavailable"] += 1
            _emit(on_progress, {"event": "item", "phase": "field_update",
                                "name": master.name, "field": field, "unavailable": True})
            if consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
                logger.warning(
                    f"[Resolver] {consecutive_failures} consecutive field-adjudication "
                    "failures — stopping for tonight, will retry next run")
                break
            continue
        consecutive_failures = 0
        judged += 1

        label = ("none_fit" if res.get("none_fit")
                 else "keep_current" if res["winner"] is None
                 else "prefers_proposal")
        stats[label] += 1
        _emit(on_progress, {
            "event": "item", "phase": "field_update", "n": judged, "total": total,
            "name": master.name, "field": field, "current": current,
            "considered": res.get("considered"), "winner": res.get("winner"),
            "label": label, "confidence": res.get("confidence"),
            "reasoning": res.get("reasoning"),
            "will_close": bool(apply and group_may_auto_apply(res, field)),
        })

        # Re-read every review the moment the model call returns. It may have
        # taken seconds or minutes, and a person may have settled one since
        # this batch was loaded — their decision wins. See
        # review_actions.record_rejection for what went wrong without this.
        live = []
        for r, c in items:
            db.refresh(r)
            if r.status == "pending":
                live.append((r, c))
        items = live
        if not items:
            continue

        for r, _c in items:
            # Keyed per field, not a flat overwrite — found alongside the
            # tags/founders bug above: a review proposing BOTH city and
            # sub_industry belongs to TWO groups, and whichever group's
            # write ran last was silently discarding the other's evidence
            # and llm_explanation entirely (2 of 125 pending rows hit this;
            # rare, but a review's evidence going missing is exactly the
            # kind of thing an unattended job must not do quietly).
            # field_adjudications self-heals regardless of processing
            # order: llm_explanation is regenerated from the FULL
            # accumulated dict every time, so once every group a review
            # belongs to has been judged, its explanation covers all of
            # them no matter which finished last.
            ev = dict(r.evidence or {})
            field_adjs = dict(ev.get("field_adjudications", {}))
            field_adjs[field] = {**res, "at": datetime.utcnow().isoformat(timespec="seconds")}
            ev["field_adjudications"] = field_adjs
            r.evidence = ev
            flag_modified(r, "evidence")
            r.llm_explanation = "\n".join(
                f"[{f}: {'keep stored' if fr['winner'] is None and not fr.get('none_fit') else 'none fit' if fr.get('none_fit') else 'prefers: ' + str(fr['winner'])}"
                f" / {fr['confidence']}] {fr['reasoning']}"
                for f, fr in field_adjs.items())[:2000]

        if apply and group_may_auto_apply(res, field):
            for r, _c in items:
                # settle_field_keep, not record_rejection: a review that also
                # proposes OTHER fields must keep them.
                outcome = settle_field_keep(db, r, field)
                if outcome == "closed":
                    stats["auto_closed"] += 1
                elif outcome == "trimmed":
                    stats["field_settled"] = stats.get("field_settled", 0) + 1
        elif apply and _earned_pick(db, res, field, items):
            _apply_earned_pick(db, master, field, res["winner"], items, stats)
        db.commit()


def _earned_pick(db, res, field, items) -> bool:
    """
    May this verdict be APPLIED rather than left for a person? Only when every
    condition holds — this is the one place the resolver ever overwrites:

      * a single winning proposal (not keep, not none_fit), at high confidence;
      * the field has EARNED autonomy from the decision ledger right now
        (processing/trust.py) — enough human agreement, over enough decisions;
      * every review in the group proposes only this field, so approving it
        cannot silently approve some other field along with it.
    """
    from processing.trust import field_autonomy

    if res.get("none_fit") or res["winner"] is None or res["confidence"] != "high":
        return False
    if any(set(_proposed(r)) != {field} for r, _c in items):
        return False
    return field_autonomy(db, field)


def _apply_earned_pick(db, master, field, winner, items, stats) -> None:
    """
    Write the winning value and settle the group. Reversible: the write lands
    in FieldChange attributed to "resolver" with the old value, so it shows in
    the record's history and can be put back by hand; rival proposals are
    suppressed exactly as a person rejecting them would.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from processing.change_log import changes_from
    from processing.review_actions import record_rejection, settle_field_keep
    from processing.storage import _sanitize_for_column

    ok, cleaned = _sanitize_for_column(field, winner)
    if not ok:
        return
    with changes_from("resolver", detail=f"earned autonomy on '{field}'"):
        setattr(master, field, cleaned)
        master.updated_at = datetime.utcnow()
        db.flush()               # inside the block, so the change is attributed

    now = datetime.utcnow()
    for r, c in items:
        if str(c.get("new")).strip() == str(winner).strip():
            # Marked BEFORE the status change, so the decision ledger reads
            # this as a machine closure and never counts it as a person
            # agreeing with the model.
            ev = dict(r.evidence or {})
            ev["auto_applied"] = {"field": field, "value": winner,
                                  "at": now.isoformat(timespec="seconds")}
            r.evidence = ev
            flag_modified(r, "evidence")
            r.status, r.resolved_at = "approved", now
        else:
            record_rejection(db, r, commit=False, by="resolver")
    db.commit()
    try:
        from api.routes.reviews import _reindex
        _reindex(db, master)
    except Exception as exc:
        logger.warning(f"[Resolver] reindex after earned apply failed: {exc}")
    stats["auto_applied"] += len(items)


async def _resolve_duplicates(db, limit: int, apply: bool, stats: dict,
                               master_ids=None, on_progress=None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from database.models import DuplicateReview, Startup
    from processing.dedup_adjudicator import adjudicate_pair, may_auto_apply
    from processing.review_actions import record_rejection, settle_field_keep
    from processing.scout_controller import scout_controller

    query = db.query(DuplicateReview).filter(
        DuplicateReview.status == "pending",
        DuplicateReview.review_type == "possible_duplicate")
    if master_ids is not None:
        query = query.filter(DuplicateReview.master_id.in_(master_ids))
    # Same rotation as the field groups above: unjudged first, then the
    # longest since judged; anything judged within resolver_rejudge_days is
    # skipped. An oldest-first .limit(N) let the reviews the model can never
    # close (same_company, insufficient_evidence) monopolise the batch.
    candidates = []
    for r in query.order_by(DuplicateReview.created_at.asc()).all():
        at = _parse_at(((r.evidence or {}).get("adjudication") or {}).get("at"))
        if _fresh(at):
            continue
        candidates.append((at or datetime.min, r.created_at or datetime.min, r))
    candidates.sort(key=lambda t: (t[0], t[1]))
    pending = [r for _a, _c, r in candidates[:limit]]

    loop = asyncio.get_event_loop()
    consecutive_failures = 0
    judged = 0
    _emit(on_progress, {"event": "phase", "phase": "possible_duplicate",
                        "total": len(pending)})

    for r in pending:
        master = db.query(Startup).filter(Startup.id == r.master_id).first()
        if master is None:
            continue
        incoming = None
        if r.incoming_id:
            incoming = db.query(Startup).filter(Startup.id == r.incoming_id).first()
        if incoming is None:
            incoming = r.incoming_data or {}  # incoming was never inserted as its own row

        async with scout_controller.gpu_mutex:
            res = await loop.run_in_executor(None, adjudicate_pair, master, incoming)

        if not res:
            consecutive_failures += 1
            stats["unavailable"] += 1
            _emit(on_progress, {"event": "item", "phase": "possible_duplicate",
                                "name": r.master_name, "unavailable": True})
            if consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
                logger.warning(
                    f"[Resolver] {consecutive_failures} consecutive duplicate-adjudication "
                    "failures — stopping for tonight, will retry next run")
                break
            continue
        consecutive_failures = 0
        judged += 1

        stats[res["verdict"]] += 1
        _emit(on_progress, {
            "event": "item", "phase": "possible_duplicate", "n": judged,
            "total": len(pending), "name": r.master_name,
            "incoming": r.incoming_name, "label": res["verdict"],
            "confidence": res.get("confidence"), "key_signal": res.get("key_signal"),
            "reasoning": res.get("reasoning"),
            "will_close": bool(apply and may_auto_apply(res)),
        })
        db.refresh(r)                       # a person may have settled it while the model ran
        if r.status != "pending":
            continue
        ev = dict(r.evidence or {})
        ev["adjudication"] = {**res, "at": datetime.utcnow().isoformat(timespec="seconds")}
        r.evidence = ev
        flag_modified(r, "evidence")
        r.llm_explanation = f"[{res['verdict']} / {res['confidence']}] {res['reasoning']}"[:2000]

        if apply and may_auto_apply(res):
            record_rejection(db, r, commit=False, by="resolver")
            stats["auto_closed"] += 1
        db.commit()


async def resolve_pending(limit: int = 120, *, apply: bool = False,
                          only_type: str = None, master_ids=None,
                          on_progress=None) -> dict:
    """
    Judge up to `limit` pending field_update groups AND up to `limit`
    pending possible_duplicate reviews (independent budgets — see this
    module's docstring for the throughput measurement behind the default).

    apply=False (the default): writes the verdict and reasoning onto every
    review judged; closes nothing.
    apply=True: additionally closes the reversible direction only — see
    this module's docstring for exactly which verdicts that is.

    only_type: "field_update" or "possible_duplicate" to run just one half
    (the CLI's --type). None (default) runs both — the nightly scheduled
    call.

    master_ids: restrict to reviews whose master_id is in this collection.
    None (default, and every production call site) processes the whole
    pending queue — this exists for tests, which run against the live
    database and must never touch real pending reviews while exercising a
    monkeypatched adjudicator. Mirrors the existing
    run_recheck_selected/run_web_verify_selected "operate on selected ids"
    convention in processing/scout_controller.py.

    Never raises. Returns a stats dict describing what happened, including
    when Ollama never responded at all (everything else comes back 0,
    "unavailable" carries the count) — a caller (the scheduler, the CLI, a
    ResolverRun row) can act on the dict without needing exception handling.
    """
    from database.connection import SessionLocal

    stats = defaultdict(int)
    db = SessionLocal()
    try:
        if only_type in (None, "field_update"):
            await _resolve_field_updates(db, limit, apply, stats,
                                         master_ids=master_ids, on_progress=on_progress)
        if only_type in (None, "possible_duplicate"):
            await _resolve_duplicates(db, limit, apply, stats,
                                      master_ids=master_ids, on_progress=on_progress)
    except Exception as exc:
        db.rollback()
        logger.error(f"[Resolver] run failed: {type(exc).__name__}: {exc}")
        stats["error"] = str(exc)
    finally:
        db.close()

    logger.info(f"[Resolver] run complete: {dict(stats)}")
    return dict(stats)
