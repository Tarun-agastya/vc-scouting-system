"""
Email delivery for the press-monitor daily digest (Phase PM, 4 Aug 2026).

Plain smtplib — no new dependency. Sends via the existing newsletter Gmail
account's SMTP + an app password (config.smtp_user / smtp_app_password),
deliberately NOT the gmail_credentials_path OAuth token used for reading
newsletters (that token is scoped gmail.readonly and cannot send mail) —
two independent credentials for two independent capabilities, so nothing
about the working newsletter-reading path is touched by adding this.
"""
from __future__ import annotations

import logging
import smtplib
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
    Corinna/Stefan never find out a real mention was caught).
    """
    from config import settings

    if not settings.smtp_user or not settings.smtp_app_password:
        raise RuntimeError(
            "smtp_user / smtp_app_password not configured — set them in .env "
            "before the press monitor can send its daily digest (see "
            "press_monitor/README.md)."
        )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"GreenTech Hub Presse-Monitor — {edition_label} ({len(matches)} Treffer)"
    msg["From"] = settings.smtp_user
    msg["To"] = ", ".join(recipients)

    body_parts = [
        f"Presse-Monitor für {edition_label} — {len(matches)} Treffer gefunden.\n",
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

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_app_password)
        server.sendmail(settings.smtp_user, recipients, msg.as_string())

    logger.info(f"[PressMonitor] Digest sent to {recipients} — {len(matches)} match(es)")
