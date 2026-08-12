"""
Shared Gmail IMAP/SMTP authentication via App Password (Phase PM, 12 Aug 2026).

Switched from OAuth (see git history, commit 72713ae) after the OAuth
client's Testing publishing status turned out to force a full interactive
browser re-consent every 7 days regardless of activity — workable for a
human-driven tool, unworkable for an unattended 8am launchd job. Moving to
Google's verified/Production OAuth tier would need a paid CASA security
assessment for gmail.readonly (a Restricted scope, not merely Sensitive) —
disproportionate for a single dummy account used by one automation. App
Passwords sidestep Google's OAuth consent machinery entirely: no
Testing/Production split, no scheduled expiry, no browser click, ever, until
the password is manually revoked.

(The ORIGINAL app-password attempt that motivated the OAuth switch was
misdiagnosed at the time as "Google's risk-scoring on programmatic
basic-auth" — git history for commit 72713ae shows the real cause was a
one-letter typo in the configured sending address, nothing wrong with app
passwords themselves. Confirmed via `git show 72713ae` before reverting to
this approach a second time.)

SETUP (manual, one-time, per credentials/README or the owner's own notes):
  1. Enable 2-Step Verification on the Gmail account, if not already on.
  2. Generate an App Password at myaccount.google.com/apppasswords.
  3. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env.

Both ingestion/newsletter_ingestor.py (IMAP, read) and
press_monitor/emailer.py (SMTP, send) import from here rather than opening
their own connections, so the credential check and error message live in
exactly one place.
"""
from __future__ import annotations

import imaplib
import logging
import smtplib

logger = logging.getLogger(__name__)


def _require_credentials():
    from config import settings

    if not settings.gmail_address or not settings.gmail_app_password:
        raise RuntimeError(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not configured. Enable "
            "2-Step Verification on the Gmail account, generate an App "
            "Password at myaccount.google.com/apppasswords, and set both "
            "in .env — see ingestion/gmail_auth.py's module docstring."
        )
    return settings


def get_imap_connection() -> imaplib.IMAP4_SSL:
    """
    Authenticated IMAP connection. Caller selects the mailbox (newsletter
    ingestion always wants "INBOX" — Gmail's category tabs, Promotions/
    Social/Updates included, are labels within the Inbox, not separate
    folders, so this alone matches the old Gmail-API search's full-mailbox
    scope) and is responsible for closing it (`conn.logout()`).
    """
    settings = _require_credentials()
    try:
        conn = imaplib.IMAP4_SSL(settings.gmail_imap_host, settings.gmail_imap_port)
        conn.login(settings.gmail_address, settings.gmail_app_password)
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(
            f"Gmail IMAP login failed for {settings.gmail_address!r}: {exc}. "
            "Check GMAIL_ADDRESS is spelled exactly right (a typo here caused "
            "the original 2026 misdiagnosis — see this module's docstring) "
            "and that GMAIL_APP_PASSWORD is a current App Password, not the "
            "account's login password."
        ) from exc
    return conn


def get_smtp_connection() -> smtplib.SMTP_SSL:
    """Authenticated SMTP connection, ready to send. Caller closes it
    (`conn.quit()`)."""
    settings = _require_credentials()
    try:
        conn = smtplib.SMTP_SSL(settings.gmail_smtp_host, settings.gmail_smtp_port)
        conn.login(settings.gmail_address, settings.gmail_app_password)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"Gmail SMTP login failed for {settings.gmail_address!r}: {exc}. "
            "Check GMAIL_ADDRESS is spelled exactly right (a typo here caused "
            "the original 2026 misdiagnosis — see this module's docstring) "
            "and that GMAIL_APP_PASSWORD is a current App Password, not the "
            "account's login password."
        ) from exc
    return conn
