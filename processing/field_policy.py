"""
Shared field-resolution policy for the review pipeline (Phase Z, 12 Aug 2026).

WHY THIS MODULE EXISTS. Two paths decide "is this incoming value worth a
human's attention": processing/storage.py's ingest diff and
processing/web_verifier.py's verdict. They had already drifted — the 12 Aug
empty-field fix landed in web_verifier only, so ingest kept staging
uncontested fills as reviews and the queue kept refilling. Putting the
policy in one importable place is the fix for that class of bug, not a
tidiness exercise.

MEASURED PROBLEM THIS SOLVES (12 Aug, live queue):
  - ~100 new field_update reviews/day, 1,443 over 14 days.
  - description + short_description alone were 996 of 1,591 field proposals
    in 7 days — 63% — because every re-crawl reworded the LLM prose just
    enough to clear _text_changed's similarity gate.
  - Sampling those conflicts showed they are NOT competing facts but
    different LEVELS OF DETAIL, and the newer value is as often a
    downgrade as an upgrade:
      Heliatek   "Organische Solarfolien."  ->  full description   (take new)
      Knowmanity full description  ->  "Preserves valuable corporate
                                        knowledge."                (keep old)
    A human clicking through 700 of those adds nothing a deterministic rule
    can't capture, which is what better_freetext() below is.

THE SAFETY RULE EVERY CALLER MUST HONOUR. A review's stored `old` is a
snapshot frozen when that review was created, NOT a live reference. The
12 Aug cleanup trusted it and silently clobbered 130 fields across 92
startups (post-mortem: scripts/revert_multi_candidate_clobbers.py). So:
  1. group by (master_id, field) across ALL pending rows before deciding;
  2. re-read the master's live value immediately before writing;
  3. never auto-apply when >1 distinct candidate value exists — hand those
     to the grouped picker (GET /reviews/grouped) instead.
This module gives you the per-value decisions; enforcing 1-3 is the
caller's job, and tests/test_field_policy.py locks in that expectation.
"""
from __future__ import annotations

import unicodedata
from typing import Optional

# Trivial rewording threshold. At or above this similarity the two texts say
# the same thing and nothing should happen at all — this is the long-standing
# behaviour of storage._text_changed, deliberately preserved.
TRIVIAL_REWORD_RATIO = 90

# How much longer the challenger must be before we switch. Below this margin
# neither value is clearly richer, so stability wins over churn — otherwise
# two near-equal descriptions ping-pong on every re-crawl forever.
LENGTH_MARGIN = 1.20

# Locale variants seen re-staging indefinitely on the live queue because
# _diff_fields compares with exact strip+lower equality. Deliberately a small
# EXPLICIT map rather than fuzzy matching: funding_stage values like "Seed"
# and "Series A" are short and must stay distinct, and a fuzzy comparator
# tuned loose enough to catch Munich/München would collapse those too.
_SYNONYMS = {
    "deutschland": "germany",
    "muenchen": "munich",
    "munchen": "munich",
    "koeln": "cologne",
    "koln": "cologne",
    "wien": "vienna",
    "zuerich": "zurich",
}


def is_empty(v) -> bool:
    """A field with nothing in it. 0 counts as empty — it's never a real
    founded_year/employee_count, only an extraction artefact."""
    return v is None or v == "" or v == 0


def split_proposal(proposed: dict) -> tuple:
    """
    Split {field: {old, new, ...}} into (fills, conflicts).

    fills     — old value was empty; nothing to adjudicate, just a gap closed.
    conflicts — a real, populated value would be overwritten; needs a human.

    NOTE the caller must still apply the multi-candidate guard from this
    module's docstring before writing a fill: "old was empty" is necessary
    but NOT sufficient, because several pending reviews can each truthfully
    report an empty old while proposing different values.
    """
    fills, conflicts = {}, {}
    for attr, p in proposed.items():
        (fills if is_empty(p.get("old")) else conflicts)[attr] = p
    return fills, conflicts


def norm_value(v) -> str:
    """
    Comparison form for non-free-text fields: NFKD diacritic-fold, lowercase,
    collapse whitespace, then map known locale synonyms. Used so a pure
    locale variant ("München" vs "Munich", "Deutschland" vs "Germany") is not
    mistaken for new information and re-staged on every single re-crawl.

    Comparison only — never write this back to a record. The stored value
    keeps its real spelling.
    """
    if v is None:
        return ""
    s = unicodedata.normalize("NFKD", str(v).strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = " ".join(s.split())
    return _SYNONYMS.get(s, s)


def _ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(a.lower(), b.lower())
    except ImportError:
        return 100.0 if a.strip().lower() == b.strip().lower() else 0.0


def better_freetext(old, new) -> Optional[str]:
    """
    Pick the more informative of two free-text values, or None for "no
    meaningful change — do nothing".

    Returns the winning STRING (which may be `old`, meaning "keep what we
    have and stop re-proposing this"), or None when the two are equivalent.

    Rules, in order:
      - new empty                  -> None   (never blank out a real value)
      - old empty                  -> new    (a plain fill)
      - similarity >= 90           -> None   (trivial reword, ignore)
      - new is >=20% longer        -> new    (a genuine upgrade)
      - otherwise                  -> old    (keep; stability beats churn)

    Length is the proxy for informativeness. It is crude but it was measured
    against the real failure cases and gets both directions right (Heliatek
    upgrade, Knowmanity downgrade) — see this module's docstring. It is also
    deterministic and explainable, which matters more here than being clever:
    this runs unattended on every ingest.
    """
    old_s = "" if old is None else str(old).strip()
    new_s = "" if new is None else str(new).strip()

    if not new_s:
        return None
    if not old_s:
        return new_s
    if _ratio(old_s, new_s) >= TRIVIAL_REWORD_RATIO:
        return None
    if len(new_s) >= len(old_s) * LENGTH_MARGIN:
        return new_s
    return old_s
