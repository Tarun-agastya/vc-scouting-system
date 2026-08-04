"""
Keyword scan over a downloaded e-paper PDF (Phase PM, 4 Aug 2026).

The PDF carries a full embedded text layer (confirmed live 4 Aug) — no OCR.
For each page whose text contains one or more watched terms, produces a
Match with the matched term(s), a text excerpt around the first match (for
the summarizer), and a rendered PNG screenshot of that page (for the email).

Matches are NOT filtered or judged here — a short/common name like
"Reisacher" WILL coincidentally match an unrelated person (confirmed live:
page 26 of the 4 Aug edition matched an artist named Reisacher, not the
company). That judgment belongs to a human glancing at the summary +
screenshot, same stance as every other extraction/verification layer in
this project — never silently drop a real hit, never silently over-trust one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

_KEYWORDS_PATH = Path(__file__).parent / "keywords.yaml"

# Higher zoom than a full-page render (Phase PM crop fix, 4 Aug) — the
# screenshot is now just the matched article's own region, not the whole
# broadsheet page, so a higher zoom stays legible without an oversized file.
_RENDER_ZOOM = 3.0
_EXCERPT_RADIUS = 900  # chars of context around a match, for the summarizer —
                        # wide enough to cover most of a real newspaper
                        # article's own body text, not just its opening line
_CROP_PADDING = 12       # points of breathing room around the article region
_COLUMN_GAP_MAX = 22     # points — vertical gap still counted as "same article"
_COLUMN_OVERLAP_MIN = 0.3  # fraction of the narrower block's width that must
                           # horizontally overlap to count as "same column"
_CROP_MAX_BLOCKS_EACH_WAY = 6  # bounds how far the merge can walk, so a
                               # column-detection fluke can't swallow the page


@dataclass
class Match:
    page_number: int          # 1-indexed, matches what a human sees in a PDF reader
    terms: List[str]
    excerpt: str
    screenshot_path: Path


def _horizontal_overlap_fraction(a, b) -> float:
    """Fraction of the NARROWER of two boxes' width that horizontally overlaps."""
    left, right = max(a.x0, b.x0), min(a.x1, b.x1)
    overlap = max(0.0, right - left)
    narrower = min(a.x1 - a.x0, b.x1 - b.x0)
    return overlap / narrower if narrower > 0 else 0.0


def _article_region(page, match_rect):
    """
    The matched article's own bounding region — the block containing the
    match, plus vertically-adjacent same-column blocks (a headline above,
    continuing body paragraphs below), not the whole page (Phase PM crop
    fix, 4 Aug 2026 — the original whole-page screenshot made a human hunt
    for the relevant paragraph on a full broadsheet page).

    Falls back to None (caller renders the whole page) if no text block can
    be matched to the search hit at all — never lets a layout-detection
    miss silently produce an unusable region.
    """
    import fitz  # PyMuPDF

    raw_blocks = page.get_text("dict").get("blocks", [])
    text_blocks = [fitz.Rect(b["bbox"]) for b in raw_blocks if b.get("lines")]
    if not text_blocks:
        return None

    text_blocks.sort(key=lambda r: r.y0)
    match_center_y = (match_rect.y0 + match_rect.y1) / 2
    match_center_x = (match_rect.x0 + match_rect.x1) / 2

    anchor_idx = None
    for i, b in enumerate(text_blocks):
        if b.y0 - 2 <= match_center_y <= b.y1 + 2 and b.x0 - 2 <= match_center_x <= b.x1 + 2:
            anchor_idx = i
            break
    if anchor_idx is None:
        return None

    included = {anchor_idx}
    anchor = text_blocks[anchor_idx]

    # Walk upward (headline / preceding context in the same column).
    cur = anchor
    for i in range(anchor_idx - 1, max(-1, anchor_idx - 1 - _CROP_MAX_BLOCKS_EACH_WAY), -1):
        b = text_blocks[i]
        gap = cur.y0 - b.y1
        if gap > _COLUMN_GAP_MAX or gap < -2:
            break
        if _horizontal_overlap_fraction(cur, b) < _COLUMN_OVERLAP_MIN:
            break
        included.add(i)
        cur = b

    # Walk downward (continuing body paragraphs in the same column).
    cur = anchor
    for i in range(anchor_idx + 1, min(len(text_blocks), anchor_idx + 1 + _CROP_MAX_BLOCKS_EACH_WAY)):
        b = text_blocks[i]
        gap = b.y0 - cur.y1
        if gap > _COLUMN_GAP_MAX or gap < -2:
            break
        if _horizontal_overlap_fraction(cur, b) < _COLUMN_OVERLAP_MIN:
            break
        included.add(i)
        cur = b

    region = text_blocks[min(included)]
    for i in included:
        region = region | text_blocks[i]
    region += (-_CROP_PADDING, -_CROP_PADDING, _CROP_PADDING, _CROP_PADDING)
    return region & page.rect  # clamp to the actual page bounds


def _regions_overlap(a, b) -> bool:
    """True if two article regions genuinely share area — used to decide
    whether two matched terms landed in the SAME article (report together)
    or two DIFFERENT articles on the same page (report as separate Match
    entries, each with its own correct excerpt/screenshot — see scan_pdf's
    docstring for why this matters)."""
    inter = a & b
    return not inter.is_empty


def load_keywords() -> List[str]:
    """Flatten keywords.yaml's categorized lists into one list, editable
    without a code change — see that file's own comment for the matching
    rules (case-insensitive literal substring, deliberately not filtered)."""
    with open(_KEYWORDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    terms: List[str] = []
    for category_terms in data.values():
        if isinstance(category_terms, list):
            terms.extend(str(t) for t in category_terms)
    return terms


def scan_pdf(pdf_path: Path, *, out_dir: Path, keywords: List[str] = None) -> List[Match]:
    """
    Scan every page of pdf_path for keywords, producing one Match per
    distinct ARTICLE that contains a hit — not one Match per page.

    This distinction matters (found during a correctness review, 4 Aug
    2026): a single newspaper page can carry two unrelated articles that
    each match a different keyword (e.g. one about "AVIA", a completely
    separate one lower on the page about a "Startup"). Grouping every term
    on a page into one Match and cropping/summarizing only the FIRST term's
    location would silently attach the wrong article's content to the
    second term — a real "missing/wrong information" risk. Instead, each
    matched term's own article region (_article_region) is computed, and
    regions that genuinely overlap are merged into one Match (multiple
    terms found in the SAME article); regions that don't overlap become
    separate Match entries, each with its own correctly-scoped excerpt and
    screenshot.

    The excerpt is now extracted via page.get_text(clip=region) — the exact
    text inside the rendered screenshot's own crop, not a character-radius
    guess from the page's linear text (which could accidentally spill into
    a neighbouring column in a multi-column layout). What the summarizer
    reads and what the screenshot shows are now guaranteed to match.

    out_dir holds the rendered screenshots (caller is responsible for
    cleaning them up after the email is sent — see run_daily.py — this
    module never deletes the PDF or its own output).
    """
    import fitz  # PyMuPDF

    keywords = keywords if keywords is not None else load_keywords()
    patterns = {kw: re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords}

    out_dir.mkdir(parents=True, exist_ok=True)
    matches: List[Match] = []

    doc = fitz.open(str(pdf_path))
    try:
        for i in range(doc.page_count):
            page = doc[i]
            text = page.get_text()
            if not text.strip():
                # A page with real visual content but zero extractable text
                # would mean this specific page is silently never scanned —
                # worth a loud warning rather than a silent skip, since that
                # would be exactly the kind of missed-information failure
                # this whole feature exists to prevent. Confirmed live 4 Aug
                # that every page of a real edition DOES carry a text layer,
                # so this should never actually fire in practice; it's a
                # tripwire in case the publisher's PDF pipeline ever changes.
                if len(page.get_images()) > 0 or page.rect.get_area() > 0:
                    logger.warning(
                        f"[PressMonitor] page {i + 1}: no extractable text "
                        "(image-only page? PDF pipeline changed?) — this "
                        "page cannot be keyword-scanned"
                    )
                continue

            hit_terms = [kw for kw, pat in patterns.items() if pat.search(text)]
            if not hit_terms:
                continue

            # One region per matched term (its first on-page occurrence),
            # then cluster overlapping regions into shared articles.
            clusters: List[dict] = []
            for kw in hit_terms:
                hits = page.search_for(kw)
                region = _article_region(page, hits[0]) if hits else None
                if region is None:
                    logger.debug(f"[PressMonitor] page {i + 1}: article region not found for {kw!r}, using full page")
                    region = page.rect
                merged_into = next((c for c in clusters if _regions_overlap(c["region"], region)), None)
                if merged_into is not None:
                    merged_into["terms"].append(kw)
                    merged_into["region"] = merged_into["region"] | region
                else:
                    clusters.append({"terms": [kw], "region": region})

            multi = len(clusters) > 1
            for idx, cluster in enumerate(clusters, 1):
                region = cluster["region"]
                excerpt = page.get_text("text", clip=region).strip()
                if not excerpt:
                    # Region ended up text-less (shouldn't happen given the
                    # page-level text.strip() check above, but stay honest
                    # rather than send the summarizer nothing to work with) —
                    # fall back to a char-radius window around the term's
                    # first offset in the page's own linear text.
                    m = patterns[cluster["terms"][0]].search(text)
                    if m:
                        start = max(0, m.start() - _EXCERPT_RADIUS)
                        end = min(len(text), m.end() + _EXCERPT_RADIUS)
                        excerpt = text[start:end].strip()

                suffix = f"_{idx}" if multi else ""
                screenshot_path = out_dir / f"page_{i + 1:03d}{suffix}.png"
                pix = page.get_pixmap(matrix=fitz.Matrix(_RENDER_ZOOM, _RENDER_ZOOM), clip=region)
                pix.save(str(screenshot_path))

                logger.info(f"[PressMonitor] page {i + 1}: matched {cluster['terms']}")
                matches.append(Match(
                    page_number=i + 1, terms=cluster["terms"], excerpt=excerpt,
                    screenshot_path=screenshot_path,
                ))
    finally:
        doc.close()

    return matches
