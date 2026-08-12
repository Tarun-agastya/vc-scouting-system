"""
Email delivery for the press-monitor daily digest (Phase PM, 4 Aug 2026).

Sends via SMTP with a Gmail App Password (ingestion/gmail_auth.py) — the
SAME credential ingestion/newsletter_ingestor.py reads with over IMAP, so
one App Password covers both. Switched back to this from the Gmail API/OAuth
(see git history, commit 72713ae, and gmail_auth.py's module docstring for
the full story) after OAuth's Testing-mode 7-day token expiry turned out to
require a human browser click on a schedule no unattended launchd job can
satisfy.
"""
from __future__ import annotations

import logging
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def send_digest(*, matches: list, edition_label: str, recipients: List[str]) -> None:
    """
    matches: list of press_monitor.scanner.Match, already summarized (each
    Match's caller attaches a .summary attribute — see run_daily.py).
    Raises on a real send failure — the caller decides how to handle it
    (this is the one step in the pipeline where silent failure would mean
    Corinna/Stefan never find out a real mention was caught). Missing/wrong
    credentials raise via ingestion.gmail_auth.get_smtp_connection with a
    message pointing at exactly what to check.
    """
    from config import settings
    from ingestion.gmail_auth import get_smtp_connection

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"GreenTech Hub Presse-Monitor — {edition_label} ({len(matches)} Treffer)"
    msg["From"] = settings.gmail_address
    msg["To"] = ", ".join(recipients)

    body_parts = [
        f"Guten Morgen! Hier die heutige Presse-Übersicht für den GreenTech Hub "
        f"({edition_label}) — {len(matches)} Treffer:\n",
    ]
    for i, m in enumerate(matches, 1):
        terms = ", ".join(m.terms)
        body_parts.append(
            f"\n{i}. Seite {m.page_number} — Treffer: {terms}\n"
            f"{getattr(m, 'summary', '(keine Zusammenfassung)')}\n"
        )
    body_parts.append(
        "\n—\nAutomatisch erstellt vom GreenTech Hub Presse-Monitor. "
        "Screenshots der jeweiligen Zeitungsseite im Anhang."
    )
    msg.attach(MIMEText("\n".join(body_parts), "plain", "utf-8"))

    for m in matches:
        path = Path(m.screenshot_path)
        if not path.exists():
            continue
        with open(path, "rb") as f:
            img = MIMEImage(f.read(), name=f"seite_{m.page_number}.png")
        img.add_header("Content-Disposition", "attachment", filename=f"seite_{m.page_number}.png")
        msg.attach(img)

    conn = get_smtp_connection()
    try:
        conn.sendmail(settings.gmail_address, recipients, msg.as_string())
    finally:
        conn.quit()

    logger.info(f"[PressMonitor] Digest sent to {recipients} — {len(matches)} match(es)")
