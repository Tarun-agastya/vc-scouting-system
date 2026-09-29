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


def record_rejection(db, review, *, commit: bool = True) -> dict:
    """
    Discard a pending review and suppress what it proposed.

      field_update       -> suppress each (master_id, field, rejected value)
      duplicate/anomaly  -> record the (master_id, incoming_id) known-different pair

    Does not check `review.status` or raise on an already-resolved review —
    that check belongs to callers that can turn it into the right response
    (an HTTP 409 for the API, a skip-and-continue for a batch job). This
    function only performs the rejection once a caller has decided to.

    `commit=False` lets a caller batch several rejections (e.g. every member
    of a duplicate-candidate group) into one transaction; the caller is then
    responsible for calling `db.commit()` itself.
    """
    from database.models import SuppressedMatch

    if review.review_type == "field_update":
        for field, change in (review.proposed_changes or {}).items():
            db.add(SuppressedMatch(
                kind="rejected_value", master_id=review.master_id,
                field=field, value=str(change.get("new")),
            ))
    else:
        if review.master_id and review.incoming_id:
            db.add(SuppressedMatch(
                kind="known_different", master_id=review.master_id,
                other_id=review.incoming_id,
            ))

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
                             "budget_exhausted"}


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

    judged = sum(v for k, v in stats.items() if k not in _RUN_STAT_NON_VERDICT_KEYS)
    auto_closed = stats.get("auto_closed", 0)

    db = SessionLocal()
    try:
        db.add(ResolverRun(
            kind=kind,
            started_at=started_at,
            finished_at=datetime.utcnow(),
            judged=judged,
            auto_closed=auto_closed,
            left_pending=max(judged - auto_closed, 0),
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
