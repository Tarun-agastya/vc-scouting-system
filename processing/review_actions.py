"""
The reject contract, in one place.

"Reject" always means the same two things: discard the pending review, and
remember the decision as a SuppressedMatch so the same value/pair doesn't
get re-flagged on the next sweep. That pairing was written out by hand at
five call sites before this module existed — `api/routes/reviews.py`'s
`_do_reject`, two grouped-resolve blocks in that same file, and both
`scripts/adjudicate_*.py` runners — which is how a future nightly resolver
would have become the sixth.

Two call sites are deliberately NOT routed through this module, because
their reject is a different shape, not a copy of this one:

  - `scripts/adjudicate_field_changes.py` suppresses only the ONE field
    being judged in that grouped call, not every field in the review's
    `proposed_changes` — a review can carry proposals for several fields,
    and this script judges one field at a time. Swapping in the contract
    below would over-suppress fields it never looked at.
  - `reviews.py`'s `resolve_grouped_reviews` / `bulk_resolve_grouped` have
    the same per-review-all-fields shape as `_do_reject` and could be
    routed through here, but the reject branch sits inline inside an
    approve/reject decision loop shared with `_apply_field_updates` and
    `_reindex`. Left alone rather than disturbed for a refactor those two
    endpoints didn't need.

So this module's job is narrower than "every reject in the codebase": it is
the contract for a caller that rejects one review at a time and wants ALL of
that review's proposed fields (or its whole pair, for a duplicate) suppressed
— which is what `_do_reject` already does, and what a resolver judging one
review at a time also needs.
"""
from datetime import datetime


def record_rejection(db, review, *, commit: bool = True, by: str = "human") -> dict:
    """
    Discard a pending review and suppress what it proposed.

      field_update       -> suppress each (master_id, field, rejected value)
      duplicate/anomaly  -> record the (master_id, incoming_id) known-different pair

    Does not check `review.status` or raise on an already-resolved review —
    that check belongs to callers that can turn it into the right response
    (an HTTP 409 for the API, a skip-and-continue for a batch job). This
    function only performs the rejection once a caller has decided to.

    `by` is who is rejecting: "human" (default) or a machine name — a machine closure
    is marked in evidence so the trust ledger can tell them apart.

    `commit=False` lets a caller batch several rejections (e.g. every member
    of a duplicate-candidate group) into one transaction; the caller is then
    responsible for calling `db.commit()` itself.
    """
    from datetime import timedelta

    from config import settings
    from database.models import SuppressedMatch

    # A machine must never overwrite a decision a person made in the meantime.
    # The resolver loads its whole batch, then spends ~40 minutes on the
    # model; a person can approve one of those reviews meanwhile, and the
    # job's copy is stale (still "pending"). Setting status="rejected" from
    # that copy flipped an approved review and suppressed the value they had
    # just applied. People are exempt: the API endpoints check status
    # themselves and turn it into a proper 409.
    if by != "human" and review.status != "pending":
        return {"status": review.status, "review_type": review.review_type, "skipped": True}

    # A machine's rejection expires; a person's never does (see
    # SuppressedMatch.expires_at for why).
    expires = (None if by == "human"
               else datetime.utcnow() + timedelta(days=settings.machine_suppression_days))

    if review.review_type == "field_update":
        for field, change in (review.proposed_changes or {}).items():
            db.add(SuppressedMatch(
                kind="rejected_value", master_id=review.master_id,
                field=field, value=str(change.get("new")), expires_at=expires,
            ))
    else:
        if review.master_id and review.incoming_id:
            db.add(SuppressedMatch(
                kind="known_different", master_id=review.master_id,
                other_id=review.incoming_id, expires_at=expires,
            ))

    if by != "human":
        # Mark it, so the decision ledger (processing/trust.py) never counts a
        # machine closure as a person agreeing with the model.
        from sqlalchemy.orm.attributes import flag_modified
        ev = dict(review.evidence or {})
        ev["auto_closed_by"] = by
        review.evidence = ev
        flag_modified(review, "evidence")

    review.status = "rejected"
    review.resolved_at = datetime.utcnow()
    if commit:
        db.commit()
    return {"status": "rejected", "review_type": review.review_type}


# Keys in a resolve_pending()/research_pending() stats dict that are NOT a
# judged-group verdict count — everything else is one (keep_current,
# prefers_proposal, none_fit, same_company, different_company,
# insufficient_evidence, ...), and the vocabulary is deliberately open-ended
# (verdicts are used as dict keys directly), so this is a denylist rather
# than an allowlist of what to sum.
_RUN_STAT_NON_VERDICT_KEYS = {"unavailable", "auto_closed", "error", "searches_used",
                             "budget_exhausted", "auto_applied"}


def record_resolver_run(kind: str, stats: dict, started_at, *, commit: bool = True) -> None:
    """
    Persist one ResolverRun row from a resolve_pending()/research_pending()
    stats dict (Phase 4, plans/REVIEW_INBOX_AUTONOMY_PLAN.md).

    Deliberately called by the SCHEDULER and the CLI only — never from
    inside review_resolver.py/review_researcher.py themselves, so those stay
    pure functions with no DB side effect beyond the reviews they actually
    judge, and so the test suite (which calls resolve_pending/research_pending
    directly, many times, scoped to throwaway test data) never leaves junk
    rows in a table that has no name column to filter test noise out by.

    `kind`: "resolve" or "research". `started_at`: pass datetime.utcnow()
    from just before the run so duration is meaningful even though the run
    itself doesn't track it.
    """
    from config import settings
    from database.connection import SessionLocal
    from database.models import ResolverRun

    # Only resolve/research runs report verdict counts. Other kinds (backup,
    # auto_merge) carry byte sizes, paths or nested dicts in stats — summing
    # those as "judged" would be meaningless or raise.
    judged = (sum(v for k, v in stats.items()
                  if k not in _RUN_STAT_NON_VERDICT_KEYS and isinstance(v, int))
              if kind in ("resolve", "research") else 0)
    auto_closed = stats.get("auto_closed", 0)

    db = SessionLocal()
    try:
        db.add(ResolverRun(
            kind=kind,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            judged=judged,
            auto_closed=auto_closed,
            left_pending=max(judged - auto_closed - stats.get("auto_applied", 0), 0),
            unavailable=stats.get("unavailable", 0),
            searches_used=stats.get("searches_used"),
            model=getattr(settings, "adjudicator_model", None) or settings.ollama_reason_model,
            error=stats.get("error"),
            stats=dict(stats),
        ))
        if commit:
            db.commit()
    finally:
        db.close()


def settle_field_keep(db, review, field: str, *, by: str = "resolver") -> str:
    """
    A machine has judged ONE field of `review` as "keep the stored value" and
    that field alone must be settled.

    A review can propose several fields at once (34 mixed tags+scalar rows sat
    in the queue). record_rejection closes the WHOLE review and suppresses
    EVERY value in it, so judging one field "keep" used to throw away the
    others too — including a lossless tags union nobody had looked at.

      only this field  -> close the review (record_rejection)
      other fields too -> suppress just this field's value, drop the key, and
                          leave the review pending for what remains

    Returns "closed", "trimmed" or "skipped" (already resolved by someone).
    """
    from datetime import timedelta

    from sqlalchemy.orm.attributes import flag_modified

    from config import settings
    from database.models import SuppressedMatch

    if review.status != "pending":
        return "skipped"
    prop = dict(review.proposed_changes or {})
    if field not in prop:
        return "skipped"
    if set(prop) == {field}:
        record_rejection(db, review, commit=False, by=by)
        return "closed"

    change = prop.pop(field)
    db.add(SuppressedMatch(
        kind="rejected_value", master_id=review.master_id, field=field,
        value=str((change or {}).get("new")),
        expires_at=datetime.utcnow() + timedelta(days=settings.machine_suppression_days)))
    review.proposed_changes = prop
    flag_modified(review, "proposed_changes")
    return "trimmed"
