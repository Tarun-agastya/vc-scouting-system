"""
Tuning-knob loader (Phase S-2) — hot-reloaded config/tuning.yaml.

Business rules (extraction include/exclude, candidate-filter keywords +
thresholds, scoring weights + tiers) live in config/tuning.yaml instead of
Python. This loader reads them with an mtime cache — the file is only re-parsed
when it actually changes on disk, so the per-chunk candidate filter stays fast.

Safety: a malformed section is ignored (built-in DEFAULTS are used for it); a
totally broken/missing file falls back to the last good parse, then to
DEFAULTS. Behaviour with no file present is identical to the old hardcoded
values, so the pipeline never breaks on a bad edit here.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUNING_YAML_PATH = os.path.join(_PROJECT_ROOT, "config", "tuning.yaml")

# ── Built-in defaults (mirror the original hardcoded values exactly) ─────────

DEFAULTS = {
    "extraction": {
        "include": (
            "any company founded in the past 20 years operating in technology, "
            "software, AI, hardware, fintech, climatetech, deeptech, proptech, "
            "logistics, or B2B SaaS — regardless of funding stage or size. "
            "VC-backed companies, unicorns, and growth-stage companies are all "
            "valid targets."
        ),
        "exclude": [
            "Traditional incumbents: established non-tech corporations such as car makers, banks, industrial conglomerates (e.g. BMW, Deutsche Bank, Siemens, McKinsey, SAP if founded before 2000).",
            "VC firms, investment funds, accelerators, media outlets.",
            "Companies in medicine, biotech, e-commerce, or food retail (exception: packaging technology).",
            "Named events, pitch days, competitions, or programs run by the accelerator/host organisation itself (e.g. 'Pitch & Match', 'Demo Day 2026', 'Gründungsnetzwerk', a dated year-suffixed program name) — these are events, not companies, even when they appear inside a list of company names.",
            "Individual people — a founder, investor, speaker, or journalist mentioned by name (e.g. 'Marc Samwer', 'Frederic Westerberg') is NOT a company, even if the surrounding sentence discusses startups. Only extract the actual company they are associated with, and only if it is itself named in the text.",
            "Article headlines, subheadings, or section titles (e.g. 'Fünf Münchner Startups und ihre Lösungen für autonomes Fahren', 'Industrie 4.0: Wie Münchner Startups die Industrie digitalisieren') are not company names — extract the actual company or companies profiled UNDER that heading, never the heading text itself.",
        ],
    },
    "candidate_filter": {
        "min_score": 2,
        "min_words": 25,
        "signals": {
            "startup": [
                "startup", "start-up", "founded", "co-founded", "founder", "ceo",
                "cto", "coo", "cpo", r"managing\s+director", "geschäftsführer",
                "raised", "funding", "funded", "investment", "investor", "venture",
                "seed", r"series\s+[a-c]", "pre-seed", "post-seed", "scale-up",
                "scaleup", "unicorn", "soonicorn", "accelerat", "incubat",
                "portfolio", "spin-?off", "spin-?out", "pitch", "traction", "mrr",
                "arr", "runway", "exit", "ipo",
            ],
            "geo": [
                "germany", "deutschland", "german", "austria", "österreich",
                "austrian", "switzerland", "schweiz", "swiss", "munich", "münchen",
                "berlin", "hamburg", "frankfurt", "cologne", "köln", "stuttgart",
                "augsburg", "nuremberg", "nürnberg", "düsseldorf", "vienna", "wien",
                "graz", "salzburg", "linz", "zurich", "zürich", "basel", "geneva",
                "genf", "bern", "europe", "european", "dach",
            ],
            "tech": [
                "platform", "software", "saas", "paas", "api", "sdk", "ai", "ml",
                "llm", r"deep\s*learning", r"machine\s*learning", "algorithm",
                "autonomous", "automation", "cloud", "data", "analytics",
                "dashboard", "sensor", "hardware", "robot", "drone", "iot",
                "marketplace", "fintech", "climatetech", "proptech", "insurtech",
                "b2b", "b2c", "b2g", "enterprise", "product", "solution",
                "technology", "digital",
            ],
            "company": [
                "GmbH", "AG", "UG", "Ltd", "Inc", "company", "companies", "firm",
                "enterprise", "venture", "team",
            ],
        },
    },
    "scoring": {
        "source_type_score": {
            "accelerator": 20, "incubator": 20, "university_hub": 20,
            "startup_network": 12, "intelligence_platform": 12,
            "newsletter": 8, "rss": 4, "general": 4,
        },
        "source_confidence_base": {
            "accelerator": 45, "incubator": 45, "university_hub": 45,
            "startup_network": 30, "intelligence_platform": 30,
            "newsletter": 20, "rss": 10, "general": 10,
        },
        "diversity_bonus": [0, 0, 6, 10, 15],
        "conf_diversity_bonus": [0, 0, 15, 25, 30],
        "tiers": [[81, "PRIORITY"], [61, "HIGH_QUALITY_LEAD"], [41, "INTERESTING"],
                  [21, "EARLY_DISCOVERY"], [0, "WEAK_SIGNAL"]],
    },
    "grounding": {
        "enabled": True,
        "check_founded_year": True,
        "check_funding_stage": True,
        "check_funding_amount": True,
        "check_employee_count": True,
        "check_founders": True,
        # A bare "is this 4-digit token anywhere in the chunk" check passes
        # on a coincidental date elsewhere in the text — found live 27 Jul
        # on a Campus Founders news-listing page where every blurb carries
        # the article's own publish date ("04/11/2024"), which the
        # extractor mistook for RiDERgy's/NextStepHR's/DeepFile's founding
        # year and the old bare-substring check couldn't tell apart from a
        # real founding statement. The year must now appear within
        # founded_year_context_chars of one of these signals.
        "founded_year_context_chars": 60,
        "founded_year_signals": [
            "founded", "founded in", "co-founded", "established", "est.",
            "incorporated", "started in", "launched in", "since",
            "gegründet", "gegr.", "seit",
        ],
        "funding_stage_keywords": [
            "pre-seed", "preseed", "vor-seed", "vorseed", "seed",
            "series a", "series b", "series c", "series d", "series e",
            "growth", "angel", "bridge", "wachstumskapital", "wachstumsrunde",
        ],
        "funding_amount_signals": [
            "million", "mio", "billion", "bn", "thousand", "tsd",
            "€", "$", "usd", "eur", "dollar", "euro",
            "funding", "raised", "investment",
            "eingesammelt", "finanzierung", "kapital", "runde",
        ],
        # Deliberately NOT "people" alone — found via Phase H-4 live testing
        # to false-positive-match unrelated text (e.g. the "Pitch & People"
        # podcast name), letting a fabricated employee_count survive.
        "employee_count_signals": [
            "employee", "employees", "headcount", "staff", "fte", "team of",
            "person team", "people on the team", "headcount of",
            "mitarbeiter", "beschäftigte", "belegschaft", "mann team", "köpfe",
        ],
    },
    "geo_scope": {
        "enabled": True,
        "non_europe_signals": [
            "china", "chinese", "beijing", "shanghai", "shenzhen", "hong kong",
            "india", "indian", "bangalore", "mumbai",
            "japan", "japanese", "tokyo",
            "south korea", "korean", "seoul",
            "singapore", "silicon valley", "san francisco",
        ],
    },
    "priority_scouting": {
        "enabled": True,
        "boost": 1,
    },
    "institutional_junk": {
        "enabled": True,
        "institution_keywords": [
            "bank", "sparkasse", "volksbank", "raiffeisenbank", "kreissparkasse",
            "stadtsparkasse",
            "versicherung", "krankenkasse", "ihk", "handwerkskammer", "kammer",
            "rechtsanwalt", "rechtsanwälte", "kanzlei", "notariat", "ministerium",
            "landkreis", "bezirksverband", "gemeinde", "stadtverwaltung", "newsflash",
        ],
        "known_incumbents": [
            "liebherr", "peri", "goldbeck", "ed. züblin", "züblin",
            "wolff und müller", "leonhard weiß", "rentschler",
        ],
        "generic_phrase_regex": r"^(ein|eine|a|an)\s+\w",
        "reject_colon": True,
        "reject_question_mark": True,
        "max_commas": 1,
        "headline_number_pattern": (
            r"^(?=.*\b(?:startups?|gr[üu]nder\w*)\b)"
            r"(zwei|drei|vier|f[üu]nf|sechs|sieben|acht|neun|zehn)\b"
        ),
    },
    "inspector": {
        "min_group_items": 4,
        "card_score_threshold": 0.55,
        "top_groups_reported": 5,
        "name_only_text_max_chars": 24,
        "name_only_min_fraction": 0.8,
        "weights": {
            "unique_identity": 0.30,
            "has_link": 0.20,
            "has_heading": 0.15,
            "homogeneity": 0.15,
            "group_size": 0.10,
            "has_image": 0.10,
            "nav_footer_cap": 0.15,   # hard ceiling, not a subtraction — see site_inspector._score_group
            "empty_text_penalty": 0.30,
        },
        "render_gain_threshold": 2.0,
        "detail_link_min_coverage": 0.6,
        "prose_link_density_max": 0.45,
    },
    "chunking": {
        # How many logo-grid company names go into one extraction call.
        # Measured 30 Jul on zollhof.de's 120-name portfolio page: at 12
        # names/chunk a call generates 12 full 17-field JSON records and
        # takes 70-75s against the extract client's 75s timeout — right at
        # the edge, so any GPU contention tips it over. A quiet-machine run
        # got 93/120 (78%); the same page under load got 62/120 (52%) with
        # 3 of 11 chunks timing out twice each. Halving the batch halves the
        # output tokens per call, moving it well clear of the timeout.
        "names_per_chunk": 6,
    },
}

# ── mtime cache ──────────────────────────────────────────────────────────────

_cache_mtime: Optional[float] = None
_cache_config: Optional[dict] = None


def _load() -> dict:
    """
    Return the parsed tuning config, re-reading the file only when its mtime
    changed. Never raises. Merges file sections over DEFAULTS so a partial or
    malformed file still yields a complete config.
    """
    global _cache_mtime, _cache_config

    try:
        mtime = os.path.getmtime(TUNING_YAML_PATH)
    except OSError:
        if _cache_config is None:
            logger.warning("[Tuning] config/tuning.yaml missing — using built-in defaults")
        return _cache_config or DEFAULTS

    if _cache_config is not None and mtime == _cache_mtime:
        return _cache_config

    try:
        with open(TUNING_YAML_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("root of tuning.yaml must be a mapping")
        merged = {
            "extraction":       {**DEFAULTS["extraction"], **(raw.get("extraction") or {})},
            "candidate_filter": {**DEFAULTS["candidate_filter"], **(raw.get("candidate_filter") or {})},
            "scoring":          {**DEFAULTS["scoring"], **(raw.get("scoring") or {})},
            "grounding":        {**DEFAULTS["grounding"], **(raw.get("grounding") or {})},
            "geo_scope":        {**DEFAULTS["geo_scope"], **(raw.get("geo_scope") or {})},
            "priority_scouting": {**DEFAULTS["priority_scouting"], **(raw.get("priority_scouting") or {})},
            "institutional_junk": {**DEFAULTS["institutional_junk"], **(raw.get("institutional_junk") or {})},
            "chunking":         {**DEFAULTS["chunking"], **(raw.get("chunking") or {})},
            "inspector":        {**DEFAULTS["inspector"], **(raw.get("inspector") or {})},
        }
        # nested weights dict: merge per-key so a partial edit keeps the rest
        insp = raw.get("inspector") or {}
        if isinstance(insp.get("weights"), dict):
            merged["inspector"]["weights"] = {
                **DEFAULTS["inspector"]["weights"], **insp["weights"]
            }
        # nested signals dict: merge per-group so a partial edit keeps the rest
        cf = raw.get("candidate_filter") or {}
        if isinstance(cf.get("signals"), dict):
            merged["candidate_filter"]["signals"] = {
                **DEFAULTS["candidate_filter"]["signals"], **cf["signals"]
            }
        _cache_config = merged
        _cache_mtime = mtime
        return merged
    except Exception as exc:
        logger.error(f"[Tuning] tuning.yaml invalid ({exc}) — using last-known-good/defaults")
        return _cache_config or DEFAULTS


# ── Public accessors ─────────────────────────────────────────────────────────

def get_extraction_rules() -> dict:
    """{'include': str, 'exclude': [str, ...]}"""
    return _load()["extraction"]


def get_candidate_filter_config() -> dict:
    """{'min_score': int, 'min_words': int, 'signals': {group: [term, ...]}, '_mtime': float}"""
    cfg = dict(_load()["candidate_filter"])
    cfg["_mtime"] = _cache_mtime  # lets candidate_filter cache compiled regexes
    return cfg


def get_scoring_config() -> dict:
    return _load()["scoring"]


def get_grounding_config() -> dict:
    """
    {'enabled': bool, 'check_founded_year': bool, ..., 'funding_stage_keywords':
    [...], 'funding_amount_signals': [...], 'employee_count_signals': [...]}
    """
    return _load()["grounding"]


def get_geo_scope_config() -> dict:
    """{'enabled': bool, 'non_europe_signals': [...], '_mtime': float}"""
    cfg = dict(_load()["geo_scope"])
    cfg["_mtime"] = _cache_mtime  # lets candidate_filter cache its compiled pattern
    return cfg


def get_priority_scouting_config() -> dict:
    """{'enabled': bool, 'boost': int, '_mtime': float}"""
    cfg = dict(_load()["priority_scouting"])
    cfg["_mtime"] = _cache_mtime  # lets candidate_filter cache its compiled pattern
    return cfg


def get_institutional_junk_config() -> dict:
    """
    {'enabled': bool, 'institution_keywords': [...], 'known_incumbents': [...],
    'generic_phrase_regex': str, 'reject_colon': bool, 'reject_question_mark':
    bool, 'max_commas': int, 'headline_number_pattern': str, '_mtime': float}
    """
    cfg = dict(_load()["institutional_junk"])
    cfg["_mtime"] = _cache_mtime  # lets qwen_client cache its compiled pattern
    return cfg


def get_chunking_config() -> dict:
    """{'names_per_chunk': int}"""
    return _load()["chunking"]


def get_inspector_config() -> dict:
    """
    Phase R-1 structural inspector settings:
    {'min_group_items': int, 'card_score_threshold': float, 'weights': {...},
     'render_gain_threshold': float, 'detail_link_min_coverage': float, ...}
    """
    return _load()["inspector"]
