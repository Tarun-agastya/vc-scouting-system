"""
Heuristic pre-filter: discard text chunks that are unlikely to contain
startup mentions before sending them to Qwen.

Goal: ~70% token reduction on typical accelerator / university hub pages
that mix navigation, legal text, blog posts, and event listings alongside
actual company profiles.

A chunk passes when it scores ≥ MIN_SCORE across four signal categories:
  STARTUP  — funding, founding, role titles, legal entity suffixes
  GEO      — European / DACH place names
  TECH     — technology, product, and market vocabulary
  COMPANY  — explicit entity markers (GmbH, "company", legal abbreviations)
"""
import re

# ── Compiled-pattern cache (rebuilt only when config/tuning.yaml changes) ──────
# The keyword groups + thresholds live in config/tuning.yaml (Phase S-2), read
# via config.tuning_loader. Regexes are compiled once and reused; they're only
# recompiled when the file's mtime changes, so this per-chunk hot path stays fast.

_cache_mtime = object()          # sentinel: forces a build on first call
_compiled: dict = {}
_min_score = 2
_min_words = 25


def _build_patterns(cfg: dict) -> None:
    global _compiled, _min_score, _min_words
    signals = cfg.get("signals") or {}
    compiled = {}
    for group, terms in signals.items():
        if not terms:
            continue
        body = "|".join(str(t) for t in terms)
        try:
            compiled[group] = re.compile(rf"\b(?:{body})\b", re.IGNORECASE)
        except re.error:
            # A bad regex fragment shouldn't kill the whole filter — skip the group.
            continue
    _compiled = compiled
    _min_score = int(cfg.get("min_score", 2))
    _min_words = int(cfg.get("min_words", 25))


def _ensure_current() -> None:
    """Reload compiled patterns iff config/tuning.yaml changed since last build."""
    global _cache_mtime
    from config.tuning_loader import get_candidate_filter_config
    cfg = get_candidate_filter_config()
    if cfg.get("_mtime") != _cache_mtime or not _compiled:
        _build_patterns(cfg)
        _cache_mtime = cfg.get("_mtime")


# ── Public API ────────────────────────────────────────────────────────────────

def is_relevant(chunk: str) -> bool:
    """
    Return True if *chunk* is likely to contain startup mentions.

    Each keyword group (startup / geo / tech / company, defined in
    config/tuning.yaml) contributes at most +1. A chunk passes when it scores
    >= min_score AND has >= min_words words. Both thresholds are configurable.
    """
    _ensure_current()

    if len(chunk.split()) < _min_words:
        return False

    score = sum(1 for pat in _compiled.values() if pat.search(chunk))
    return score >= _min_score
