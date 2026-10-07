"""
Fetch a startup's own logo from its website — deterministic HTML parsing, not
a vision model. gemma4:12b (templates/one_pager/llm.py) is a text model; it is
never shown images and cannot "look at" a page to find a logo. What actually
works for this, and what real logo-fetching tools (favicon services, CRM
enrichment tools) do, is reading the page's own markup: sites already mark
their logo for browsers and search engines (apple-touch-icon, a header <img>
named "logo", a favicon) — that is a more reliable source than asking a model
to guess, and it costs no GPU time and no web-search credits.

PRIORITY, best mark first (a company's real logo, not a generic icon):
  1. <link rel="apple-touch-icon">      — usually the cleanest square mark
  2. a header/nav <img> whose src/alt/class names it as the logo
  3. <link rel="icon"> (the favicon)    — universal but often low quality
  4. /favicon.ico at the site root
  5. Clearbit's free logo lookup (logo.clearbit.com) — a public, keyless,
     unlimited service; last resort, and recorded in the YAML's sources like
     every other third-party fact this tool pulls in.
Each candidate is validated (really an image, not a 1x1 tracking pixel, not a
wide banner) before being accepted; the first that passes wins, so this never
fetches more than it needs to.

Never raises. A failure just means no logo file — the same "degrade, don't
block the draft" rule as the rest of this tool (deck.py's images, llm.py's
drafting, websearch.py's search).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 3 * 1024 * 1024       # a logo is never legitimately bigger than this
MAX_SIDE_PX = 800                        # downscale anything larger
MIN_SIDE_PX = 24                         # reject tracking pixels / blank icons
MAX_ASPECT = 6.0                         # reject banners mistaken for a logo
TIMEOUT_S = 10.0

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
}
_LOGO_HINT = re.compile(r"logo", re.I)
# A <link rel="icon"> is only trusted if its FILE NAME looks like an icon.
# Found live (Eidola, Oct 2026): their site builder pointed the favicon tag at
# a renamed project photograph ("Tectonic-Dusts...ico", a pile of rock) — a
# valid, correctly-sized image, and entirely the wrong thing to print as a
# company's logo. Image analysis can't tell (a logo on a textured background
# and a photo score alike); the source can. Better no logo — which is obvious
# and a one-minute fix — than a plausible wrong one.
_ICON_NAME_HINT = re.compile(r"(favicon|icon|logo|\d+x\d+)", re.I)


def fetch_logo(website: str, out_path: Path) -> dict:
    """
    Try to save the company's logo to `out_path` (always a .png). Returns
    {"found": bool, "source": str, "path": Path | None, "note": str | None}.
    `source` names where it came from, for the YAML's sources/open_questions.
    """
    if not website:
        return {"found": False, "source": None, "path": None, "note": None}

    base = website if "://" in website else f"https://{website}"
    try:
        import httpx

        with httpx.Client(timeout=TIMEOUT_S, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(base)
            resp.raise_for_status()
            html = resp.text
            final_url = str(resp.url)
    except Exception as exc:
        # The page itself is unreadable (bot-blocked 403, down, TLS) — but a
        # domain-based lookup needs no page, so still try it (Hula Earth's
        # site returns 403 to scripts, yet its logo is a lookup away).
        logger.info(f"[logo_fetch] could not load {base} ({type(exc).__name__})")
        clearbit = _try_clearbit(base, out_path)
        if clearbit:
            return clearbit
        return {"found": False, "source": None, "path": None,
                "note": f"The website could not be read ({type(exc).__name__}) and no logo lookup had it."}

    for url, source in _candidates(html, final_url):
        saved = _try_save(url, out_path, source)
        if saved:
            return saved

    clearbit = _try_clearbit(final_url, out_path)
    if clearbit:
        return clearbit

    return {"found": False, "source": None, "path": None,
            "note": "No logo could be found on the website or the favicon services tried."}


def _rel_tokens(tag) -> set:
    """BeautifulSoup normalises <link rel="..."> to a list per the HTML5 spec
    (it's a space-separated token list), regardless of how the markup wrote
    it — so this is the one correct way to test it, lower-cased."""
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {r.lower() for r in rel}


def _candidates(html: str, page_url: str):
    """Yield (absolute_url, source_label) in priority order, best first."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all("link", href=True):
        if _rel_tokens(tag) & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
            yield urljoin(page_url, tag["href"]), "the website's apple-touch-icon"

    header = soup.find("header") or soup.find(attrs={"class": _LOGO_HINT}) or soup
    seen = set()
    for scope in (header, soup):          # header first, then the whole page
        for img in scope.find_all("img", src=True):
            hay = " ".join(str(img.get(a, "")) for a in ("src", "alt", "class", "id"))
            if img["src"] not in seen and _LOGO_HINT.search(hay):
                seen.add(img["src"])
                yield urljoin(page_url, img["src"]), "a logo image on the website"

    for tag in soup.find_all("link", href=True):
        if _rel_tokens(tag) & {"icon", "shortcut icon"}:
            name = urlparse(tag["href"]).path.rsplit("/", 1)[-1]
            if _ICON_NAME_HINT.search(name):
                yield urljoin(page_url, tag["href"]), "the website's favicon"

    root = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    yield urljoin(root, "/favicon.ico"), "the website's default favicon"


def _try_save(url: str, out_path: Path, source: str) -> Optional[dict]:
    try:
        import httpx

        with httpx.Client(timeout=TIMEOUT_S, headers=_HEADERS, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception:
        return None
    if not data or len(data) > MAX_FETCH_BYTES:
        return None

    img = _load_image(data, url, resp.headers.get("content-type", ""))
    if img is None:
        return None
    w, h = img.size
    if w < MIN_SIDE_PX or h < MIN_SIDE_PX:
        return None
    if max(w, h) / max(1, min(w, h)) > MAX_ASPECT:
        return None

    _save_png(img, out_path)
    return {"found": True, "source": source, "path": out_path, "note": None}


def _load_image(data: bytes, url: str, content_type: str):
    """Bytes -> a Pillow image, handling SVG (rasterized via fitz, already a
    dependency — see deck.py) since Pillow cannot open vector formats."""
    from PIL import Image

    is_svg = url.lower().endswith(".svg") or "svg" in content_type.lower()
    if is_svg:
        try:
            import fitz

            doc = fitz.open(stream=data, filetype="svg")
            pix = doc[0].get_pixmap(alpha=True, matrix=fitz.Matrix(4, 4))
            return Image.open(__import__("io").BytesIO(pix.tobytes("png")))
        except Exception:
            return None
    try:
        im = Image.open(__import__("io").BytesIO(data))
        im.load()
        return im
    except Exception:
        return None


def _save_png(img, out_path: Path) -> None:
    from PIL import Image

    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")
    w, h = img.size
    if max(w, h) > MAX_SIDE_PX:
        scale = MAX_SIDE_PX / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


def save_local(src, out_path: Path) -> bool:
    """A logo file someone uploaded (png/jpg/svg/...): validate and normalise it
    to the same PNG form as a fetched one. Size and banner checks are skipped —
    a person chose this file on purpose."""
    try:
        data = Path(src).read_bytes()
    except OSError:
        return False
    img = _load_image(data, str(src), "")
    if img is None:
        return False
    _save_png(img, out_path)
    return True


def _try_clearbit(page_url: str, out_path: Path) -> Optional[dict]:
    """Last resort: Clearbit's free, keyless logo lookup by domain. Public
    service, no account, no quota — but still a third-party source, so it is
    reported like one (generate.py adds it to the YAML's `sources:`)."""
    domain = urlparse(page_url).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain:
        return None
    url = f"https://logo.clearbit.com/{domain}?size=256"
    saved = _try_save(url, out_path, "Clearbit logo lookup (third-party, by domain)")
    return saved
