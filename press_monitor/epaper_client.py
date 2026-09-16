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

# The dashboard element this whole function exists to click. Waiting for
# THIS is what tells us the post-login page is ready — see the networkidle
# note in download_todays_edition.
_EDITION_LINK_SELECTOR = 'a[href^="javascript:pdfDownloadClickHandler"]'


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
    from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright

    target_date = target_date or date.today()
    date_str = target_date.strftime("%d.%m.%Y")   # matches the site's own "Di., 04.08.2026" display format's date part
    edition_date_param = target_date.strftime("%Y%m%d")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            # Deliberately NOT wait_until="networkidle" (changed 16 Sep 2026).
            # networkidle needs 500ms with no in-flight requests, which a page
            # carrying analytics/ads/tracking may never reach — and when it
            # doesn't, Playwright raises and the whole digest is lost for the
            # day. That is exactly what happened on 16 Sep: the 08:00 run died
            # on `wait_for_load_state("networkidle")` with a hard
            # TimeoutError while the 15 Sep run had succeeded, on unchanged
            # code and unchanged credentials. Nothing about the page was
            # broken; one request simply never went quiet.
            #
            # So each wait is now for the specific element the next step needs,
            # which is both faster and can't be defeated by a chatty tracker.
            await page.goto(
                _LOGIN_URL.format(region=region), wait_until="domcontentloaded",
                timeout=25_000,
            )
            await _dismiss_cookie_banner(page)
            await page.wait_for_selector("#inputUsername", timeout=25_000)
            await page.fill("#inputUsername", email)
            await page.fill("#inputPassword", password)
            await page.click('button[type="submit"], input[type="submit"]')

            # Wait for the edition download link itself. If it never appears we
            # must NOT simply report "not published yet", because a failed
            # login looks identical from here — and quietly mislabelling a
            # credential problem as an expected non-event is how a month of
            # missing digests would go unnoticed. So distinguish the two: the
            # login form still being on screen means we never got in.
            try:
                await page.wait_for_selector(
                    _EDITION_LINK_SELECTOR, timeout=40_000, state="attached",
                )
            except PlaywrightTimeout:
                still_on_login = await page.locator("#inputUsername").count()
                if still_on_login:
                    raise RuntimeError(
                        "[EPaper] Login appears to have failed — the login form is "
                        "still present after submitting. Check EPAPER_EMAIL / "
                        "EPAPER_PASSWORD in .env, and whether the subscription is "
                        "still active."
                    )
                logger.warning(
                    "[EPaper] Logged in, but no edition download link appeared within "
                    "40s — treating as 'not published yet'. If this repeats for days, "
                    "the dashboard markup has probably changed."
                )
            await _dismiss_cookie_banner(page)

            # Find today's edition ID dynamically from the dashboard — the ID
            # (e.g. "523415") changes every day and isn't guessable; it lives
            # on the "Gesamtausgabe (PDF) laden" link next to today's date
            # inside the hero card.
            handler_call = await page.evaluate(
                """
                ([dateStr, sel]) => {
                  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                  let node;
                  while (node = walker.nextNode()) {
                    if (node.textContent.includes(dateStr)) {
                      let el = node.parentElement;
                      for (let i = 0; i < 6 && el; i++) {
                        const a = el.querySelector && el.querySelector(sel);
                        if (a) return a.getAttribute('href');
                        el = el.parentElement;
                      }
                    }
                  }
                  return null;
                }
                """,
                [date_str, _EDITION_LINK_SELECTOR],
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
