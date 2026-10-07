"""
Read a startup's own website — and tell its own website apart from pages
ABOUT it — for one-pagers built from the HubDrive database instead of a deck.

WHY. A database record is a lead, not a source: it was extracted
automatically, and measured on the first three records asked for (Oct 2026)
it carried a news article as Atira's "website", a politician among Atira's
founders, no website at all for Bliro and REEcover, and junk tags. With no
deck, the company's own site is the most reliable source for what it does,
where it sits and who runs it — and reading it costs no search credits.

WHAT IS READ, all plain HTTP (free):
  * the homepage, plus up to MAX_SUBPAGES internal pages whose link looks like
    about / team / company / product / technology — and the IMPRINT
    (Impressum), which in Germany and Switzerland legally states the
    registered address: the most reliable "location" there is;
  * third-party pages (a news article from the database, web search hits)
    only as EXCERPTS: the paragraphs that name the company, plus the one after
    each. A newsletter or listing page names many companies, and taking the
    whole page is exactly how one company's facts land on another
    (the pipeline's recorded cross-attribution bug).

Never raises; a site that can't be read just contributes nothing.
Same isolation contract as the rest of this tool: no pipeline imports.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

TIMEOUT_S = 12.0
MAX_SUBPAGES = 4
PAGE_CHARS = 2500            # per page, after extraction
SITE_CHARS = 9000            # whole site
EXCERPT_CHARS = 2500         # per third-party page
MAX_IMAGES = 6

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Link text or path that points at the pages worth reading, best first.
_SUBPAGE_HINTS = [
    r"impressum|imprint|legal[-_ ]?notice",
    r"about|über[-_ ]?uns|ueber[-_ ]?uns|wir|company|unternehmen",
    r"team|founders|gründer",
    r"product|produkt|solution|lösung|loesung|technolog|platform|plattform|how[-_ ]it[-_ ]works",
]
_SKIP_LINK = re.compile(r"(login|signin|sign-in|cart|privacy|datenschutz|cookie|career|jobs|"
                        r"karriere|blog/|news/|\.pdf$|mailto:|tel:|#)", re.I)

# Domain prefixes/suffixes startups commonly add to their name (getbliro, atira-ai).
_NAME_SUFFIXES = ("ai", "hq", "app", "io", "tech", "labs", "group", "gmbh", "ag", "energy",
                  "materials", "systems", "solutions", "technologies", "robotics", "bio")
_NAME_PREFIXES = ("get", "try", "use", "join", "go", "the", "with")
_LEGAL_WORDS = re.compile(r"\b(gmbh|ag|ug|se|inc|ltd|llc|kg|co|haftungsbeschränkt|"
                          r"haftungsbeschraenkt)\b\.?", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def norm_name(name: str) -> str:
    return _norm(_LEGAL_WORDS.sub(" ", str(name)))


def host(url: str) -> str:
    h = urlparse(url if "//" in str(url) else f"//{url}").netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def is_official_domain(url: str, name: str) -> bool:
    """
    True when the domain IS the company's name: atira.ai, bliro.io,
    eidolamaterials.ch, hula.earth, getbliro.com. A listing or news site
    (munich-startup.de, tech.eu) never matches, whatever it says about them.
    """
    n = norm_name(name)
    h = host(url)
    if not n or not h:
        return False
    labels = h.split(".")
    first = _norm(labels[0])
    candidates = {first, _norm("".join(labels[:-1])), _norm("".join(labels))}
    if n in candidates:
        return True
    if any(first == n + s for s in _NAME_SUFFIXES) or any(first == p + n for p in _NAME_PREFIXES):
        return True
    # "REEcover AG" -> reecover; "Hula Earth" -> hula.earth handled above.
    return False


def discover_website(name: str, urls: List[str]) -> Optional[str]:
    """The first URL (in result order) whose domain is the company's own."""
    for u in urls:
        if is_official_domain(u, name):
            p = urlparse(u if "//" in u else f"https://{u}")
            return f"{p.scheme or 'https'}://{p.netloc}/"
    return None


@dataclass
class SiteRead:
    url: str
    pages: List[tuple] = field(default_factory=list)      # (url, text)
    image_urls: List[str] = field(default_factory=list)
    note: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(f"[Page: {u}]\n{t}" for u, t in self.pages)


def _get(url: str):
    import httpx

    with httpx.Client(timeout=TIMEOUT_S, headers=_HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r


def _extract(html: str, url: str) -> str:
    try:
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True) or ""
    except Exception:
        text = ""
    return " ".join(text.split())[:PAGE_CHARS] if text else ""


def read_site(url: str) -> SiteRead:
    """Homepage + the most informative subpages of the company's own site."""
    from bs4 import BeautifulSoup

    base = url if "://" in url else f"https://{url}"
    out = SiteRead(url=base)
    try:
        r = _get(base)
    except Exception as exc:
        out.note = f"The website could not be read ({type(exc).__name__})."
        return out
    home_url = str(r.url)
    soup = BeautifulSoup(r.text, "html.parser")
    home_text = _extract(r.text, home_url)
    if home_text:
        out.pages.append((home_url, home_text))
    out.image_urls = _image_candidates(soup, home_url)

    own = host(home_url)
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(home_url, a["href"]).split("#")[0]
        if host(href) != own or href.rstrip("/") == home_url.rstrip("/") or _SKIP_LINK.search(href):
            continue
        label = f"{a.get_text(' ', strip=True)} {urlparse(href).path}"
        rank = next((i for i, pat in enumerate(_SUBPAGE_HINTS) if re.search(pat, label, re.I)), None)
        if rank is not None and href not in [l for _, l in links]:
            links.append((rank, href))
    links.sort(key=lambda t: t[0])

    used = len(home_text)
    for _, href in links[:MAX_SUBPAGES]:
        if used >= SITE_CHARS:
            break
        try:
            pr = _get(href)
        except Exception:
            continue
        t = _extract(pr.text, href)
        if t and t[:200] not in home_text:
            t = t[: SITE_CHARS - used]
            out.pages.append((str(pr.url), t))
            used += len(t)
    if not out.pages:
        out.note = "The website loaded but no readable text was found (it may be built entirely in JavaScript)."
    return out


def name_excerpt(text: str, name: str, max_chars: int = EXCERPT_CHARS) -> str:
    """
    Only the paragraphs of a third-party page that name the company, each with
    the paragraph after it (where the facts about it usually continue).
    """
    if not text:
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    pat = re.compile(rf"(?<!\w){re.escape(str(name).strip())}(?!\w)", re.I)
    keep = set()
    for i, p in enumerate(paras):
        if pat.search(p):
            keep.update({i, i + 1})
    chosen = [paras[i] for i in sorted(keep) if i < len(paras)]
    return " ".join(" ".join(chosen).split())[:max_chars]


def read_article(url: str, name: str) -> tuple:
    """A third-party page (news article, profile) cut down to name_excerpt,
    plus every link on it — an article about a startup usually links to its
    site, which is a free way to find a website the database doesn't have."""
    if not url or not str(url).startswith(("http://", "https://")):
        return "", []
    try:
        import trafilatura
        from bs4 import BeautifulSoup

        r = _get(url)
        text = trafilatura.extract(r.text, url=str(r.url)) or ""
        links = [urljoin(str(r.url), a["href"]) for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True)]
    except Exception:
        return "", []
    return name_excerpt(text, name), links


def _image_candidates(soup, page_url: str) -> List[str]:
    urls = []
    for m in soup.find_all("meta"):
        if (m.get("property") or m.get("name") or "").lower() in ("og:image", "twitter:image") and m.get("content"):
            urls.append(urljoin(page_url, m["content"]))
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        hay = " ".join(str(img.get(a, "")) for a in ("src", "alt", "class"))
        if re.search(r"logo|icon|avatar|flag|badge|sprite|pixel", hay, re.I):
            continue
        urls.append(urljoin(page_url, src))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:20]


def save_site_images(image_urls: List[str], out_dir: Path, max_n: int = MAX_IMAGES) -> List[str]:
    """
    Download the website's larger images as visual CANDIDATES (a person still
    picks the two for the page, as with deck images). Small images, icons and
    banners are skipped. Returns the saved file names.
    """
    from io import BytesIO
    from PIL import Image

    saved = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for u in image_urls:
        if len(saved) >= max_n:
            break
        try:
            data = _get(u).content
            if len(data) > 8 * 1024 * 1024:
                continue
            im = Image.open(BytesIO(data))
            im.load()
        except Exception:
            continue
        w, h = im.size
        if w < 480 or h < 270 or max(w, h) / max(1, min(w, h)) > 3.0:
            continue
        im = im.convert("RGB")
        if w > 1500:
            im = im.resize((1500, int(h * 1500 / w)))
        name = f"web{len(saved) + 1:02d}.jpg"
        im.save(out_dir / name, "JPEG", quality=84, optimize=True)
        saved.append(name)
    return saved
