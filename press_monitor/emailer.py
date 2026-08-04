"""
Email delivery for the press-monitor daily digest (Phase PM, 4 Aug 2026).

Sends via the Gmail API (ingestion/gmail_auth.py) using the SAME OAuth
token/consent flow as ingestion/newsletter_ingestor.py — one consent grants
both read (newsletters) and send (this) capability, so nothing about the
newsletter-reading path is affected by adding this.

Switched to this (from SMTP + an app password, see git history) after the
app-password path proved unreliable for this Gmail account even with
valid, freshly-generated credentials — Google's risk-scoring on
programmatic basic-auth sign-in is independent of anything actually being
misconfigured, and OAuth is what Google itself recommends over app
passwords anyway.
"""
from __future__ import annotations

import base64
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
    Corinna/Stefan never find out a real mention was caught). A missing/
    expired OAuth token raises via ingestion.gmail_auth.get_gmail_service
    with a message pointing at what to do (re-run to reopen the browser
    consent flow), same as the newsletter reader's existing behaviour.
    """
    from ingestion.gmail_auth import get_gmail_service

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"GreenTech Hub Presse-Monitor — {edition_label} ({len(matches)} Treffer)"
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

    service = get_gmail_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    logger.info(f"[PressMonitor] Digest sent to {recipients} — {len(matches)} match(es)")
