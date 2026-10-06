"""
processing/digest.py — pure text, no mail is ever sent from a test. The point
is that a quiet failure shows up as a line in this mail, so each way the
nightly chain can silently die must produce a visible problem.
"""
from datetime import datetime

import processing.backup as backup
from database.models import ResolverRun
from processing.digest import build_digest, send_digest


def test_a_missing_backup_is_a_problem_in_the_subject(db, monkeypatch):
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: None)
    subject, body = build_digest(db)
    assert subject.startswith("⚠") and "NO COMPLETE BACKUP" in body


def test_a_stale_backup_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: 90.0)
    assert "90h old" in build_digest(db)[1]


def test_a_healthy_chain_says_all_ok(db, monkeypatch):
    from config import settings
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: 6.5)
    monkeypatch.setattr(settings, "resolver_enabled", False)     # judged on its own merits below
    subject, body = build_digest(db)
    assert "NEEDS ATTENTION" not in body and "Backup: ok" in body


def test_resolver_on_but_silent_is_a_problem(db, monkeypatch):
    """The silent-death case: switched on, never ran. Silence must be loud."""
    from config import settings
    monkeypatch.setattr(backup, "latest_backup_age_hours", lambda: 5.0)
    monkeypatch.setattr(settings, "resolver_enabled", True)
    # No resolve run in the window: use an hours=0 window so nothing matches.
    assert "did not run" in build_digest(db, hours=0)[1]


def test_no_recipients_means_nothing_is_sent(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "digest_recipients", "")
    assert "skipped" in send_digest()


def test_a_failed_send_is_reported_not_raised(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "digest_recipients", "nobody@example.invalid")

    def boom():
        raise RuntimeError("smtp down")
    monkeypatch.setattr("ingestion.gmail_auth.get_smtp_connection", boom)
    assert "error" in send_digest()
