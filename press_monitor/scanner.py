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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

_KEYWORDS_PATH = Path(__file__).parent / "keywords.yaml"

# Rendered at 2x scale for a legible screenshot without an unreasonably large
# attachment — a broadsheet page at 1x is often too small to read once
# emailed and viewed on a phone.
_RENDER_ZOOM = 2.0
_EXCERPT_RADIUS = 400  # chars of context around a match, for the summarizer


@dataclass
class Match:
    page_number: int          # 1-indexed, matches what a human sees in a PDF reader
    terms: List[str]
    excerpt: str
    screenshot_path: Path


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
    Scan every page of pdf_path for keywords, rendering a screenshot for
    each matched page. out_dir holds the rendered screenshots (caller is
    responsible for cleaning them up after the email is sent — see
    run_daily.py — this module never deletes the PDF or its own output).
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
                continue

            hit_terms = [kw for kw, pat in patterns.items() if pat.search(text)]
            if not hit_terms:
                continue

            first_match = patterns[hit_terms[0]].search(text)
            start = max(0, first_match.start() - _EXCERPT_RADIUS)
            end = min(len(text), first_match.end() + _EXCERPT_RADIUS)
            excerpt = text[start:end].strip()

            screenshot_path = out_dir / f"page_{i + 1:03d}.png"
            pix = page.get_pixmap(matrix=fitz.Matrix(_RENDER_ZOOM, _RENDER_ZOOM))
            pix.save(str(screenshot_path))

            logger.info(f"[PressMonitor] page {i + 1}: matched {hit_terms}")
            matches.append(Match(
                page_number=i + 1, terms=hit_terms, excerpt=excerpt,
                screenshot_path=screenshot_path,
            ))
    finally:
        doc.close()

    return matches
