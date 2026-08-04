"""
Press-monitor daily run (Phase PM, 4 Aug 2026): download today's Memminger
Zeitung e-paper edition, scan it for GreenTech Hub / portfolio / partner
mentions, and email a digest (summary + page screenshot per match) to
config.press_monitor_recipients — only when there's at least one match, so
Corinna/Stefan aren't getting an empty email every single day.

Usage
-----
  python -m press_monitor.run_daily              # today's edition
  python -m press_monitor.run_daily --date 2026-08-03

Never writes to the VC-scouting Postgres/Qdrant — this is a fully
independent feature sharing only the local Ollama summarizer and (per the
owner's own instruction) the existing newsletter Gmail account's sending
capability. Safe to run standalone (cron/launchd) or from the FastAPI
scheduler (see api/main.py).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKDIR = Path(__file__).parent / "_runs"


async def run(target_date: date = None) -> dict:
    """
    Returns a small result dict (never raises for the "expected" outcomes —
    not published yet, zero matches — only for real failures: bad login,
    SMTP misconfigured, etc., which the caller/scheduler should log loudly).
    """
    from config import settings
    from press_monitor import epaper_client, scanner, summarizer, emailer

    target_date = target_date or date.today()

    if not settings.epaper_email or not settings.epaper_password:
        raise RuntimeError(
            "epaper_email / epaper_password not configured — set them in .env "
            "(see press_monitor/README.md)."
        )

    run_dir = _WORKDIR / target_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = run_dir / "edition.pdf"
    screenshots_dir = run_dir / "pages"

    try:
        downloaded = await epaper_client.download_todays_edition(
            email=settings.epaper_email,
            password=settings.epaper_password,
            region=settings.epaper_region,
            publication=settings.epaper_publication,
            out_path=pdf_path,
            target_date=target_date,
        )
        if downloaded is None:
            logger.info(f"[PressMonitor] No edition published for {target_date} yet — skipping")
            return {"status": "not_published", "date": target_date.isoformat()}

        matches = scanner.scan_pdf(pdf_path, out_dir=screenshots_dir)
        if not matches:
            logger.info(f"[PressMonitor] {target_date}: no watched terms found")
            return {"status": "no_matches", "date": target_date.isoformat()}

        for m in matches:
            m.summary = summarizer.summarize_match(m.terms[0], m.excerpt)

        recipients = [r.strip() for r in settings.press_monitor_recipients.split(",") if r.strip()]
        edition_label = f"{settings.epaper_publication} {target_date.strftime('%d.%m.%Y')}"
        emailer.send_digest(matches=matches, edition_label=edition_label, recipients=recipients)

        return {
            "status": "sent", "date": target_date.isoformat(),
            "match_count": len(matches),
            "pages": [m.page_number for m in matches],
            "recipients": recipients,
        }
    finally:
        # Never retain a local copy of the full copyrighted edition or its
        # page renders beyond what was needed to send the digest.
        shutil.rmtree(run_dir, ignore_errors=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to today")
    args = ap.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    result = asyncio.run(run(target))
    print(result)


if __name__ == "__main__":
    main()
