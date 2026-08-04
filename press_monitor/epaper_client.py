"""
Allgäuer/Memminger Zeitung e-paper client (Phase PM, 4 Aug 2026).

Logs in as a real subscriber (Playwright — the site is a normal login-gated
web app, no public API) and downloads the current day's full edition as a
PDF via the site's own "Gesamtausgabe (PDF) laden" feature — the exact same
action a subscriber takes manually. The PDF carries a full embedded text
layer (confirmed live 4 Aug against a real edition), so no OCR is needed:
press_monitor/scanner.py reads it directly with PyMuPDF.

Never logs in more than once per run; never attempts to reach paywalled
content without first authenticating as the real subscriber.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DASHBOARD_URL = "https://webepaper.allgaeuer-zeitung.de/dashboard.act?region={region}"
_LOGIN_URL = (
    "https://webepaper.allgaeuer-zeitung.de/redirectlogin.act"
    "?region={region}&calleeAction=dashboard.act&loginOnly=1"
)
_COOKIE_ACCEPT_PATTERNS = ("Alle akzeptieren", "Akzeptieren", "Accept All", "Zustimmen")


async def _dismiss_cookie_banner(page) -> None:
    """Best-effort — never raises, mirrors ingestion/web_scraper.py's convention."""
    for pat in _COOKIE_ACCEPT_PATTERNS:
        try:
            loc = page.locator(f'button:has-text("{pat}")').first
            if await loc.count() and await loc.is_visible(timeout=1000):
                await loc.click(timeout=2000)
                return
        except Exception:
            continue


async def download_todays_edition(
    *, email: str, password: str, region: str, publication: str,
    out_path: Path, target_date: Optional[date] = None,
) -> Optional[Path]:
    """
    Log in and download the current (or target_date) edition's full PDF.

    Returns the saved path, or None if today's edition isn't listed yet
    (e.g. the e-paper hasn't published today's issue) — never raises for
    that case, since it's an expected daily-scheduling race, not an error.
    """
    from playwright.async_api import async_playwright

    target_date = target_date or date.today()
    date_str = target_date.strftime("%d.%m.%Y")   # matches the site's own "Di., 04.08.2026" display format's date part
    edition_date_param = target_date.strftime("%Y%m%d")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            await page.goto(
                _LOGIN_URL.format(region=region), wait_until="networkidle", timeout=25_000,
            )
            await _dismiss_cookie_banner(page)
            await page.fill("#inputUsername", email)
            await page.fill("#inputPassword", password)
            await page.click('button[type="submit"], input[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=25_000)
            await _dismiss_cookie_banner(page)

            # Find today's edition ID dynamically from the dashboard — the ID
            # (e.g. "523415") changes every day and isn't guessable; it lives
            # on the "Gesamtausgabe (PDF) laden" link next to today's date
            # inside the hero card.
            handler_call = await page.evaluate(
                """
                (dateStr) => {
                  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                  let node;
                  while (node = walker.nextNode()) {
                    if (node.textContent.includes(dateStr)) {
                      let el = node.parentElement;
                      for (let i = 0; i < 6 && el; i++) {
                        const a = el.querySelector && el.querySelector('a[href^="javascript:pdfDownloadClickHandler"]');
                        if (a) return a.getAttribute('href');
                        el = el.parentElement;
                      }
                    }
                  }
                  return null;
                }
                """,
                date_str,
            )
            if not handler_call:
                logger.warning(f"[EPaper] No edition found for {date_str} — not published yet, or date format mismatch")
                return None

            # handler_call looks like: javascript:pdfDownloadClickHandler('Memminger Zeitung', '523415', 'ME', '20260804')
            js_call = handler_call.removeprefix("javascript:")

            async with page.expect_download(timeout=30_000) as dl_info:
                await page.evaluate(js_call)
            download = await dl_info.value

            out_path.parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(str(out_path))
            logger.info(f"[EPaper] Downloaded {publication} {date_str} -> {out_path}")
            return out_path
        finally:
            await browser.close()
