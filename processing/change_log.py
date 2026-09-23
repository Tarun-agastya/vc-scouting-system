"""
Per-record change history, captured at one place.

Every field write in this system goes through a SQLAlchemy session flush, so
that is where the history is taken — one `before_flush` hook rather than a
logging call in each writer. The alternative loses by construction: add a
`log_change()` next to every `setattr` and one day someone adds a writer and
forgets, and the gap is invisible, because a history that quietly stops being
complete looks exactly like a record that stopped changing.

Volume and what is worth keeping
--------------------------------
A sweep touches thousands of fields. Most of that is machine bookkeeping —
scores recomputed, timestamps bumped, embeddings re-pointed — and logging it
would bury the handful of changes a person cares about under noise they can
neither act on nor interpret. So the set below is an allowlist of fields with
human meaning, not a denylist of noisy ones: a new internal column added later
is silently ignored, which is the safe default.

Attribution
-----------
The hook cannot know WHY something changed, only that it did. Callers declare
it with the `changes_from` context manager:

    with changes_from("merge", detail=str(review_id)):
        ...

Unattributed writes are recorded as "system" rather than dropped — an
unexplained change is still worth knowing about, and an unlabelled row is a
prompt to attribute that caller next time.
"""
import contextvars
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Fields with human meaning. An allowlist, deliberately: anything not named
# here is ignored, so adding an internal column later cannot start polluting
# the timeline.
TRACKED_FIELDS = {
    "name", "website", "short_description", "description",
    "industry", "sub_industry", "tech_cluster", "tags",
    "country", "city", "address", "founded_year", "employee_count",
    "funding_stage", "total_funding_usd", "contact_info", "linkedin",
    "business_model", "is_gmbh",
    "verification_status", "interest_status",
}

_MAX_VALUE_CHARS = 600          # a readable trail, not a backup

_ctx = contextvars.ContextVar("change_source", default=("system", None))


@contextmanager
def changes_from(source: str, detail: str = None):
    """Attribute every field change written inside this block."""
    token = _ctx.set((source, detail))
    try:
        yield
    finally:
        _ctx.reset(token)


def _render(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict, tuple)):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[:_MAX_VALUE_CHARS] + "…"


def install(session_factory) -> None:
    """
    Attach the hook to a sessionmaker. Idempotent.

    Registered once at import of database.connection so every session in the
    process is covered — including scripts, which is the point: a bulk merge
    run from the command line should land in the same history as a click.
    """
    from sqlalchemy import event
    from sqlalchemy.orm.attributes import get_history

    if getattr(session_factory, "_change_log_installed", False):
        return

    @event.listens_for(session_factory, "before_flush")
    def _capture(session, flush_context, instances):
        from database.models import FieldChange, Startup

        source, detail = _ctx.get()
        rows = []
        for obj in session.dirty:
            if not isinstance(obj, Startup) or not session.is_modified(obj):
                continue
            for field in TRACKED_FIELDS:
                try:
                    hist = get_history(obj, field)
                except Exception:
                    continue
                if not hist.has_changes():
                    continue
                old = hist.deleted[0] if hist.deleted else None
                new = hist.added[0] if hist.added else None
                if _render(old) == _render(new):
                    continue        # a rewrite of the same value is not a change
                rows.append(FieldChange(
                    startup_id=obj.id, field=field,
                    old_value=_render(old), new_value=_render(new),
                    source=source, detail=detail,
                ))
        if rows:
            session.add_all(rows)

    session_factory._change_log_installed = True
    logger.info("[ChangeLog] field-change capture installed")


def history_for(db, startup_id, limit: int = 60) -> list:
    """Newest first. Used by the detail panel's timeline."""
    from database.models import FieldChange

    rows = (db.query(FieldChange)
            .filter(FieldChange.startup_id == startup_id)
            .order_by(FieldChange.changed_at.desc())
            .limit(max(1, min(limit, 500))).all())
    return [{
        "field": r.field,
        "old": r.old_value or None,
        "new": r.new_value or None,
        "source": r.source,
        "detail": r.detail,
        "changed_at": r.changed_at.isoformat() if r.changed_at else None,
    } for r in rows]


def prune(db, keep_per_record: int = 100, older_than_days: int = None) -> int:
    """
    Keep the history bounded. Returns how many rows were removed.

    Two limits because they catch different things: `keep_per_record` stops a
    single much-recrawled company dominating the table, and `older_than_days`
    stops the table growing forever across all records.
    """
    from sqlalchemy import func

    from database.models import FieldChange

    removed = 0
    if older_than_days:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        removed += db.query(FieldChange).filter(
            FieldChange.changed_at < cutoff).delete(synchronize_session=False)

    noisy = (db.query(FieldChange.startup_id, func.count(FieldChange.id).label("n"))
             .group_by(FieldChange.startup_id)
             .having(func.count(FieldChange.id) > keep_per_record).all())
    for startup_id, _n in noisy:
        keep = [r.id for r in (db.query(FieldChange.id)
                               .filter(FieldChange.startup_id == startup_id)
                               .order_by(FieldChange.changed_at.desc())
                               .limit(keep_per_record).all())]
        removed += db.query(FieldChange).filter(
            FieldChange.startup_id == startup_id,
            ~FieldChange.id.in_(keep)).delete(synchronize_session=False)
    db.commit()
    return removed
