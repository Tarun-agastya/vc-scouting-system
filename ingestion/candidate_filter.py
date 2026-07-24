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

Then a separate, independent hard filter (geo_scope, Phase 23 Jul) rejects a
chunk outright if it has a clear non-Europe signal and no DACH/Europe geo
signal at all — see is_relevant()'s docstring for why this exists as a
deterministic check rather than a prompt instruction.
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

_geo_scope_mtime = object()
_non_europe_pattern = None       # compiled regex, or None when disabled
_geo_scope_enabled = True


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


def _build_geo_scope(cfg: dict) -> None:
    global _non_europe_pattern, _geo_scope_enabled
    _geo_scope_enabled = bool(cfg.get("enabled", True))
    terms = cfg.get("non_europe_signals") or []
    if not terms:
        _non_europe_pattern = None
        return
    body = "|".join(str(t) for t in terms)
    try:
        _non_europe_pattern = re.compile(rf"\b(?:{body})\b", re.IGNORECASE)
    except re.error:
        _non_europe_pattern = None


def _ensure_current() -> None:
    """Reload compiled patterns iff config/tuning.yaml changed since last build."""
    global _cache_mtime, _geo_scope_mtime
    from config.tuning_loader import get_candidate_filter_config, get_geo_scope_config
    cfg = get_candidate_filter_config()
    if cfg.get("_mtime") != _cache_mtime or not _compiled:
        _build_patterns(cfg)
        _cache_mtime = cfg.get("_mtime")

    geo_cfg = get_geo_scope_config()
    if geo_cfg.get("_mtime") != _geo_scope_mtime:
        _build_geo_scope(geo_cfg)
        _geo_scope_mtime = geo_cfg.get("_mtime")


# ── Public API ────────────────────────────────────────────────────────────────

def is_relevant(chunk: str) -> bool:
    """
    Return True if *chunk* is likely to contain startup mentions.

    Each keyword group (startup / geo / tech / company, defined in
    config/tuning.yaml) contributes at most +1. A chunk passes when it scores
    >= min_score AND has >= min_words words. Both thresholds are configurable.

    Geo-scope hard reject (independent of the score above): a chunk with a
    clear non-Europe signal (China, Beijing, Tokyo, ...) and NO DACH/Europe
    geo signal at all is rejected outright, before ever reaching the LLM.
    This exists because the extraction prompt's exclude rules alone don't
    reliably stop the small extraction model from pulling in out-of-scope
    companies — confirmed live (23 Jul): a German-newsletter profile of a
    Chinese AI founder (Moonshot AI) was still extracted despite an explicit
    "exclude non-Europe" prompt rule. A chunk that genuinely IS about a
    non-European company expanding into Europe naturally mentions a specific
    European place (e.g. "opening a Berlin office"), which satisfies the geo
    carve-out and lets it through untouched.
    """
    _ensure_current()

    if len(chunk.split()) < _min_words:
        return False

    if _geo_scope_enabled and _non_europe_pattern is not None:
        geo_pattern = _compiled.get("geo")
        has_non_europe = bool(_non_europe_pattern.search(chunk))
        has_europe = bool(geo_pattern.search(chunk)) if geo_pattern else False
        if has_non_europe and not has_europe:
            return False

    score = sum(1 for pat in _compiled.values() if pat.search(chunk))
    return score >= _min_score
