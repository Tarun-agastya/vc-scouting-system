"""
Site inspector CLI — READ-ONLY. Never writes to Postgres, Qdrant or config.

Shows what the pipeline structurally sees on a page: how it fetches it, how
much content each extraction mode would yield, and (from Phase R-1 onward)
which repeating card groups it detects and which strategy it would choose.

This is the tool for answering "why did this site extract badly?" without
running an ingestion and without a human eyeballing raw HTML — the manual
step that produced the two `render_mode: always` entries in sources.yaml.

Usage
-----
  python scripts/inspect_site.py <url> [--render] [--json] [--save out.html]
  python scripts/inspect_site.py --source-id zollhof
  python scripts/inspect_site.py --all            # every registered web source

Exit code is always 0 — this is a diagnostic, not a gate.
"""
import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402


def _fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


async def inspect(url: str, *, force_render: bool, save: str = None) -> dict:
    """Fetch a page both ways where useful and report what each mode yields."""
    from ingestion.web_scraper import WebScraper, _extract_text, _is_junk_alt
    from bs4 import BeautifulSoup

    scraper = WebScraper()
    report = {"url": url}

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # Static fetch, unconditionally — the comparison is the point.
        static_html = ""
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                static_html = resp.text
            report["static_status"] = resp.status_code
        except Exception as exc:
            report["static_error"] = str(exc)

        static_text = _extract_text(static_html) if static_html else ""
        report["static_html_chars"] = len(static_html)
        report["static_text_chars"] = len(static_text)

        rendered_html = ""
        if force_render:
            try:
                rendered_html = await scraper._fetch_page(client, url, force_render=True)
            except Exception as exc:
                report["render_error"] = str(exc)
        rendered_text = _extract_text(rendered_html) if rendered_html else ""
        report["rendered_html_chars"] = len(rendered_html)
        report["rendered_text_chars"] = len(rendered_text)

    html = rendered_html or static_html
    if save and html:
        with open(save, "w") as fh:
            fh.write(html)
        report["saved_to"] = save

    if not html:
        report["error"] = "no HTML retrieved"
        return report

    # What the alt harvest sees, before and after the junk filter.
    soup = BeautifulSoup(html, "html.parser")
    raw_alts, clean_alts, seen = [], [], set()
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        k = alt.lower()
        if len(alt) < 2 or k in seen:
            continue
        seen.add(k)
        raw_alts.append(alt)
        if not _is_junk_alt(alt):
            clean_alts.append(alt)
    report["alt_raw"] = len(raw_alts)
    report["alt_clean"] = len(clean_alts)
    report["alt_sample"] = clean_alts[:15]
    report["alt_dropped_sample"] = [a for a in raw_alts if a not in clean_alts][:10]

    report["links_total"] = len(soup.find_all("a", href=True))
    report["images_total"] = len(soup.find_all("img"))

    # Phase R-1's structural inspector, when it exists. Kept optional so this
    # CLI is useful from R-0 onward rather than only after R-1 lands.
    try:
        from ingestion.site_inspector import probe_html, derive_strategy_deterministic
        from config.tuning_loader import get_inspector_config
        signals = probe_html(html, url)
        report["signals"] = signals.to_dict() if hasattr(signals, "to_dict") else str(signals)
        strat = derive_strategy_deterministic(signals, get_inspector_config())
        report["deterministic_strategy"] = (
            strat.to_dict() if hasattr(strat, "to_dict") else str(strat)
        )
    except ImportError:
        report["signals"] = "(site_inspector not built yet — Phase R-1)"

    return report


def _print(report: dict) -> None:
    print("=" * 74)
    print(report["url"])
    print("=" * 74)
    if "error" in report:
        print(f"  ERROR: {report['error']}")
        return
    static_t = report.get("static_text_chars", 0)
    rend_t = report.get("rendered_text_chars", 0)
    print(f"  static  : {_fmt(report.get('static_html_chars',0)):>10} html chars "
          f"-> {_fmt(static_t):>8} text chars")
    if rend_t:
        gain = f"{rend_t/static_t:.1f}x" if static_t else "n/a"
        print(f"  rendered: {_fmt(report.get('rendered_html_chars',0)):>10} html chars "
              f"-> {_fmt(rend_t):>8} text chars   (gain {gain})")
    print(f"  images  : {report.get('images_total',0)}   links: {report.get('links_total',0)}")
    print(f"  alt text: {report.get('alt_raw',0)} raw -> {report.get('alt_clean',0)} after junk filter")
    if report.get("alt_sample"):
        print(f"    kept   : {report['alt_sample']}")
    if report.get("alt_dropped_sample"):
        print(f"    dropped: {report['alt_dropped_sample']}")
    if isinstance(report.get("signals"), str):
        print(f"  structure: {report['signals']}")
    elif report.get("signals"):
        print(f"  structure: {json.dumps(report['signals'], indent=4)[:1400]}")
    if report.get("deterministic_strategy"):
        print(f"  strategy : {report['deterministic_strategy']}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only site structure inspector")
    ap.add_argument("url", nargs="?", help="URL to inspect")
    ap.add_argument("--source-id", help="Inspect a registered source by id")
    ap.add_argument("--all", action="store_true", help="Inspect every registered web source")
    ap.add_argument("--render", action="store_true", help="Also fetch with a headless browser")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    ap.add_argument("--save", help="Write the fetched HTML to this path")
    args = ap.parse_args()

    from config.source_loader import get_web_sources

    targets = []
    if args.all:
        targets = [(s.primary_url, getattr(s, "render_mode", "auto") == "always")
                   for s in get_web_sources()]
    elif args.source_id:
        match = [s for s in get_web_sources() if s.source_id == args.source_id]
        if not match:
            print(f"No source with id {args.source_id!r}")
            return
        targets = [(match[0].primary_url, getattr(match[0], "render_mode", "auto") == "always")]
    elif args.url:
        targets = [(args.url, args.render)]
    else:
        ap.print_help()
        return

    reports = []
    for url, render in targets:
        rep = asyncio.run(inspect(url, force_render=render or args.render, save=args.save))
        reports.append(rep)
        if not args.json:
            _print(rep)

    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
