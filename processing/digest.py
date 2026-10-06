"""
Daily owner digest (A5 of the autonomy plan).

An unattended system that decides things needs to tell someone what it
decided, and — more importantly — needs its SILENCE to mean something. The
press digest is a different feature for different people (see
press_monitor/README.md); it is deliberately isolated from the scouting
database and is not a heartbeat. So this is sent from the API process itself,
which already owns the database and the mail credentials.

It always reports the things that make a quiet failure visible: whether last
night's backup exists and is fresh, whether the resolver actually ran when it
is switched on, and whether anything was skipped or blocked. A day with no
email is itself the alarm.

Off by default: `settings.digest_recipients` is empty, and nothing is sent
until someone sets an address. Never defaults to any existing recipient list.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def build_digest(db, hours: int = 26) -> tuple:
    """(subject, plain-text body) for the last `hours`. Pure: no sending."""
    from sqlalchemy import func

    from config import settings
    from database.models import DuplicateReview, MergeSnapshot, ResolverRun
    from processing.auto_merge import MAX_BACKUP_AGE_HOURS
    from processing.backup import latest_backup_age_hours
    from processing.trust import autonomy_report

    since = datetime.utcnow() - timedelta(hours=hours)
    problems, lines = [], []

    # ── backup: the precondition for every destructive step ────────────────
    age = latest_backup_age_hours()
    last_backup = (db.query(ResolverRun).filter(ResolverRun.kind == "backup")
                   .order_by(ResolverRun.started_at.desc()).first())
    if age is None:
        problems.append("NO COMPLETE BACKUP EXISTS — auto-merge is blocked.")
    elif age > MAX_BACKUP_AGE_HOURS:
        problems.append(f"Latest backup is {age:.0f}h old (limit {MAX_BACKUP_AGE_HOURS}h) — auto-merge is blocked.")
    else:
        lines.append(f"Backup: ok, {age:.1f}h old.")
    if last_backup and last_backup.error:
        problems.append(f"Last backup run failed: {last_backup.error}")

    # ── what ran ───────────────────────────────────────────────────────────
    runs = (db.query(ResolverRun).filter(ResolverRun.started_at >= since)
            .order_by(ResolverRun.started_at).all())
    by_kind = {}
    for r in runs:
        by_kind.setdefault(r.kind, []).append(r)

    if settings.resolver_enabled and "resolve" not in by_kind:
        problems.append("The review resolver is ON but did not run in this window.")
    for r in by_kind.get("resolve", []):
        lines.append(f"Resolver: judged {r.judged}, closed {r.auto_closed} "
                     f"(+{(r.stats or {}).get('auto_applied', 0)} applied on earned fields), "
                     f"{r.left_pending} left for you, {r.unavailable} skipped.")
        if r.error:
            problems.append(f"Resolver stopped early: {r.error}")
        if r.unavailable and not r.judged:
            problems.append("Resolver could not reach the model — nothing was judged.")
    for r in by_kind.get("research", []):
        lines.append(f"Research: {r.judged} judged, {r.searches_used or 0} searches, "
                     f"{r.auto_closed} closed.")

    merges = by_kind.get("auto_merge", [])
    for r in merges:
        st = r.stats or {}
        if st.get("blocked"):
            problems.append(f"Auto-merge blocked: {st['blocked']}")
        else:
            lines.append(f"Auto-merge: {st.get('merged', 0)} merged, {st.get('failed', 0)} failed, "
                         f"{st.get('moot', 0)} duplicate reviews closed as moot.")
    recent = db.query(MergeSnapshot).filter(MergeSnapshot.created_at >= since,
                                            MergeSnapshot.undone_at.is_(None)).count()
    if recent:
        lines.append(f"{recent} merge(s) can still be undone from the Review Inbox (Recent merges).")

    # ── the queue and the trust ledger ─────────────────────────────────────
    pending = dict(db.query(DuplicateReview.review_type, func.count())
                   .filter(DuplicateReview.status == "pending")
                   .group_by(DuplicateReview.review_type).all())
    lines.append("Pending for you: " + (", ".join(f"{n} {t}" for t, n in pending.items()) or "nothing") + ".")

    earned = [f["field"] for f in autonomy_report(db) if f["earned"]]
    lines.append("Auto-applying overwrites on: " + (", ".join(earned) if earned else
                 f"no field yet (needs {settings.trust_min_decisions} human decisions "
                 f"at >={round(settings.trust_min_agreement * 100)}% agreement)") + ".")

    subject = ("⚠ " if problems else "") + "Scouting nightly: " + (
        f"{len(problems)} problem(s)" if problems else "all ok")
    body = ""
    if problems:
        body += "NEEDS ATTENTION\n" + "\n".join(f"  ! {p}" for p in problems) + "\n\n"
    body += "\n".join(lines)
    body += "\n\nDashboard: http://localhost:8000/dashboard/#/reviews"
    return subject, body


def send_digest() -> dict:
    """Send tonight's digest. Never raises; a failure is returned, and logged."""
    from config import settings
    from database.connection import SessionLocal

    recipients = [r.strip() for r in (settings.digest_recipients or "").split(",") if r.strip()]
    if not recipients:
        return {"skipped": "digest_recipients is empty"}

    db = SessionLocal()
    try:
        subject, body = build_digest(db)
    finally:
        db.close()

    try:
        from email.mime.text import MIMEText

        from ingestion.gmail_auth import get_smtp_connection

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = subject, settings.gmail_address, ", ".join(recipients)
        conn = get_smtp_connection()
        try:
            conn.sendmail(settings.gmail_address, recipients, msg.as_string())
        finally:
            conn.quit()
        return {"sent": len(recipients), "subject": subject}
    except Exception as exc:
        logger.error(f"[Digest] send failed: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
