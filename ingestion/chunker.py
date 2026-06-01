"""
Sentence-boundary text chunker with overlap.

Splits long documents into overlapping chunks sized for Qwen's effective
extraction window (~1500 tokens ≈ 6000 chars of mixed English/German text).

Phase 3: replaces the old hard-truncation at 12 000 chars so the full
content of every crawled page is processed rather than silently dropped.
"""
from typing import List

# 1 token ≈ 4 chars for mixed English/German text
CHUNK_SIZE = 6_000   # ~1 500 tokens — fits comfortably in an 8 192-token context
OVERLAP    =   800   # ~200 tokens  — enough to avoid splitting mid-entity


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
    A 30 000-char crawl of 5 pages produces ~5 chunks of ≤ 6 000 chars
    instead of being truncated after the first 12 000 chars.
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
