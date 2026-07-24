"""
Sentence-boundary text chunker with overlap.

Splits long documents into overlapping chunks sized for Qwen's effective
extraction window (~1500 tokens ≈ 6000 chars of mixed English/German text).

Phase 3: replaces the old hard-truncation at 12 000 chars so the full
content of every crawled page is processed rather than silently dropped.
"""
import re
from typing import List

# 1 token ≈ 4 chars for mixed English/German text
CHUNK_SIZE = 1_800   # ~450 tokens — fits inside a 4 096-token Qwen context with headroom for prompt + schema
OVERLAP    =   250   # ~62 tokens  — enough to avoid splitting mid-entity without duplicating content


def split(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = OVERLAP,
) -> List[str]:
    """
    Split *text* into overlapping chunks, snapping cut-points to clean
    paragraph / line / sentence boundaries when possible.

    Algorithm
    ---------
    * Advance by ``step = chunk_size - overlap`` each iteration so the
      next chunk re-reads the tail of the previous one.
    * Before committing a cut-point, search backward up to 300 chars for
      the nearest ``\\n\\n``, ``\\n``, or ``'. '`` boundary so we never
      split a sentence mid-word.
    * Chunks shorter than 100 chars (nav bars, breadcrumbs) are skipped.

    Examples
    --------
    A 30 000-char crawl of 5 pages produces ~17 chunks of ≤ 1 800 chars
    each processed with a 4 096-token Qwen context.
    """
    text = text.strip()
    if not text:
        return []

    step     = max(1, chunk_size - overlap)   # how far start advances each iter
    text_len = len(text)
    chunks: List[str] = []
    start = 0

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # Snap cut-point to a clean boundary (only when not already at end)
        if end < text_len:
            snap_from = max(start + step, end - 300)
            for sep in ("\n\n", "\n", ". "):
                idx = text.rfind(sep, snap_from, end)
                if idx != -1:
                    end = idx + len(sep)
                    break

        chunk = text[start:end].strip()
        if len(chunk) >= 100:
            chunks.append(chunk)

        start += step

    return chunks


def split_blurbs(text: str, min_chars: int = 40, max_chars: int = 3000) -> List[str]:
    """
    Split newsletter-style text into one company blurb per chunk, on
    blank-line boundaries, with NO overlap (Phase H-1).

    Unlike `split()` (a sliding window built for long unstructured web
    pages), a newsletter digest is a sequence of short, independent,
    blank-line-delimited items — typically one headline line immediately
    followed by its body paragraph, with a blank line before the next
    company. Splitting on blank lines keeps each blurb (headline + body)
    together as its own chunk.

    This matters because feeding several companies to the extractor in one
    call let it bleed a fact from one company's paragraph onto its neighbor
    (e.g. a founding year stated for company A getting attached to company B
    right below it) — and the sliding-window's overlap could duplicate a
    single blurb across two chunks, causing the same company to be extracted
    twice with two different (both wrong) guesses. One blurb per call, no
    overlap, eliminates both failure modes.

    Blocks under `min_chars` (nav links, "Anzeige", a lone headline with no
    body yet) are merged forward so a body paragraph is never separated from
    its own headline. A block over `max_chars` (an intro/greeting section
    with no blank-line breaks) falls back to `split()` so no single call
    ever exceeds the model's context.
    """
    text = text.strip()
    if not text:
        return []

    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]

    blocks: List[str] = []
    buffer = ""
    for block in raw_blocks:
        candidate = f"{buffer}\n{block}".strip() if buffer else block
        if len(candidate) < min_chars:
            buffer = candidate  # too short alone — carry forward, merge with next
            continue
        if len(candidate) > max_chars:
            blocks.extend(split(candidate))
        else:
            blocks.append(candidate)
        buffer = ""

    if buffer:  # trailing short fragment with nowhere left to merge forward
        if blocks:
            blocks[-1] = f"{blocks[-1]}\n{buffer}".strip()
        else:
            blocks.append(buffer)

    return blocks


# Marker web_scraper._extract_text() appends before a harvested block of
# <img alt="..."> names (logo-grid portfolio pages) — see that function's
# docstring. Matched here, not imported, to avoid a chunker<->scraper
# circular import; if the marker text ever changes it must change in both.
_LOGO_GRID_MARKER = "\n\nPortfolio / logo grid entries on this page:\n"


def split_web_page(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = OVERLAP,
    names_per_chunk: int = 12,
) -> List[str]:
    """
    Chunk a crawled web page for extraction — the general entry point
    ingestion/worker_queue.py uses for every web-sourced page.

    Identical to plain split() for ordinary pages. The one difference: if
    the page carries a harvested "logo grid" name block (a portfolio page
    where each company appears ONLY as a logo image, name in alt text —
    see web_scraper._extract_text), that block is split into small batches
    of names instead of running through the char-based sliding window with
    the rest of the prose.

    Why this matters (confirmed live 24 Jul on zollhof.de's startup
    portfolio): the name block is short enough in raw characters to fit in
    ONE sliding-window chunk even with 100+ names in it — but asking the
    extraction call to return a full 17-field record for 100+ companies in
    one response blows past both the model's output-token budget and the
    request timeout, so the ENTIRE chunk fails and zero of those names ever
    become records, even though every one of them was successfully captured
    in the text. Splitting into ~12-name batches keeps each call the same
    shape (and cost) as an ordinary chunk. Grounding (H-1) still applies
    per batch, so a name-only chunk correctly yields name-only stub
    records — no fields get invented for information that was never there.
    """
    if _LOGO_GRID_MARKER not in text:
        return split(text, chunk_size, overlap)

    prose, _, name_block = text.partition(_LOGO_GRID_MARKER)
    chunks = split(prose, chunk_size, overlap) if prose.strip() else []

    names = [n.strip() for n in name_block.strip().split("\n") if n.strip()]
    for i in range(0, len(names), names_per_chunk):
        batch = names[i:i + names_per_chunk]
        chunks.append(
            "The following are company/startup names shown as logos in a "
            "portfolio grid, with no further description available:\n"
            + "\n".join(batch)
        )
    return chunks
