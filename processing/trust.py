"""
Earn autonomy by track record (A4 of the autonomy plan).

The resolver only ever acts on the reversible direction. Everything else —
"the model prefers this proposed value" — waits for a person, because
overwriting a stored value with a worse one is the failure this database has
actually suffered. That policy is right until there is evidence to change it,
and until now there was no way to get evidence: nobody recorded whether the
person clicking agreed with the model.

This module records that (one flush hook, see install()), and turns it into a
number per field. A field earns auto-apply of a high-confidence pick only when

    agreement >= settings.trust_min_agreement     (default 95%)
    over >= settings.trust_min_decisions          (default 30)
    distinct (record, field) decisions in the last trust_window_days (90)

and it is recomputed from the ledger at the moment of use, so it is revoked
automatically the moment agreement slips — there is no "granted" state that
can go stale. Identity fields (website, name) never qualify, whatever the
numbers. Every field starts with no autonomy.

What is and isn't graded
------------------------
  * Only reviews a PERSON closed. Anything the resolver, researcher or
    auto-merge closed carries an `auto_*` marker in evidence and is skipped —
    the model must not grade its own homework.
  * Only reviews with exactly ONE proposed field. On a multi-field review the
    status is one flag for several independent choices, so it cannot say
    which the human agreed with; recording a guess would poison the ledger.
    (2 of 125 pending rows today.)
  * none_fit is never recorded: it makes no claim, so it cannot be right or
    wrong.
  * Counted per (master, field): three sibling reviews proposing three
    values for one field are ONE decision, not three.
"""
import contextvars
import functools
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# evidence keys that mean "a machine closed this, not a person"
_MACHINE_MARKERS = ("auto_closed_by", "auto_applied", "auto_merge", "auto_merge_moot",
                    "policy_closed", "auto_filled", "auto_filled_at")


# True while a BULK endpoint is running. "Approve all matching filter" is one
# click over hundreds of reviews; counting each as an independent person
# agreeing with the model would let rubber-stamping earn auto-apply — the very
# thing the gate exists to prevent.
_in_bulk = contextvars.ContextVar("trust_in_bulk", default=False)


def not_a_judgement(fn):
    """Decorator for async bulk endpoints: decisions made inside are not graded."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        token = _in_bulk.set(True)
        try:
            return await fn(*args, **kwargs)
        finally:
            _in_bulk.reset(token)
    return wrapper


def _audit_row(review):
    """DecisionAudit kwargs for a just-human-resolved review, or None."""
    if review.review_type != "field_update":
        return None
    ev = review.evidence or {}
    if any(m in ev for m in _MACHINE_MARKERS):
        return None
    prop = review.proposed_changes or {}
    if len(prop) != 1:
        return None
    (field, change), = prop.items()
    adj = (ev.get("field_adjudications") or {}).get(field)
    if not adj or adj.get("none_fit") or not isinstance(change, dict):
        return None

    winner = adj.get("winner")
    if winner is None:
        verdict, agreed = "keep", review.status == "rejected"
    else:
        verdict = "prefer"
        this_is_the_pick = str(change.get("new")).strip() == str(winner).strip()
        # A review carrying the picked value should be approved; one carrying
        # a rival value should be rejected — either way "agreed" is the human
        # ending up where the model said.
        agreed = (review.status == "approved") if this_is_the_pick else (review.status == "rejected")
    return dict(review_id=review.id, master_id=review.master_id, field=field, verdict=verdict,
                confidence=(adj.get("confidence") or "low"), agreed=bool(agreed))


def install(session_factory) -> None:
    """Attach the hook to a sessionmaker. Idempotent. See database/connection.py."""
    from sqlalchemy import event
    from sqlalchemy.orm.attributes import get_history

    if getattr(session_factory, "_trust_installed", False):
        return

    @event.listens_for(session_factory, "before_flush")
    def _capture(session, flush_context, instances):
        from database.models import DecisionAudit, DuplicateReview

        if _in_bulk.get():
            return
        rows = []
        for obj in session.dirty:
            if not isinstance(obj, DuplicateReview) or obj.status not in ("approved", "rejected"):
                continue
            hist = get_history(obj, "status")
            if not hist.added:
                continue
            # pending -> resolved. The old value is only known if the row was
            # loaded fresh; an instance that was committed earlier in the same
            # session is EXPIRED, and SQLAlchemy then records no old value at
            # all. Every resolve path stamps resolved_at alongside status, so
            # a newly-set resolved_at is the second signal — otherwise the
            # ledger would silently record nothing depending on how the
            # object happened to be loaded.
            was_pending = bool(hist.deleted) and hist.deleted[0] == "pending"
            just_resolved = not hist.deleted and bool(get_history(obj, "resolved_at").added)
            if not (was_pending or just_resolved):
                continue
            kwargs = _audit_row(obj)
            if kwargs:
                rows.append(DecisionAudit(**kwargs))
        if rows:
            session.add_all(rows)

    session_factory._trust_installed = True
    logger.info("[Trust] decision ledger installed")


def agreement(db, field: str, days: int = None) -> dict:
    """{'n': distinct decisions, 'agreed': how many, 'rate': 0-1 or None}."""
    from sqlalchemy import func

    from config import settings
    from database.models import DecisionAudit

    days = days or settings.trust_window_days
    rows = (db.query(DecisionAudit.master_id, func.bool_and(DecisionAudit.agreed))
            .filter(DecisionAudit.field == field,
                    DecisionAudit.verdict == "prefer",
                    DecisionAudit.confidence == "high",
                    DecisionAudit.decided_at >= datetime.utcnow() - timedelta(days=days))
            .group_by(DecisionAudit.master_id).all())
    n = len(rows)
    agreed = sum(1 for _m, ok in rows if ok)
    return {"n": n, "agreed": agreed, "rate": (agreed / n) if n else None}


def field_autonomy(db, field: str) -> bool:
    """May the resolver apply a high-confidence 'prefer' pick on this field?"""
    from config import settings
    from processing.field_adjudicator import IDENTITY_FIELDS

    if field in IDENTITY_FIELDS:
        return False
    a = agreement(db, field)
    return a["n"] >= settings.trust_min_decisions and a["rate"] >= settings.trust_min_agreement


def autonomy_report(db) -> list:
    """Per field: how the ledger stands and whether it currently qualifies."""
    from config import settings
    from database.models import DecisionAudit

    fields = sorted({f for (f,) in db.query(DecisionAudit.field).distinct().all() if f})
    out = []
    for f in fields:
        a = agreement(db, f)
        out.append({"field": f, **a, "earned": field_autonomy(db, f),
                    "needs": settings.trust_min_decisions,
                    "threshold": settings.trust_min_agreement})
    return out
