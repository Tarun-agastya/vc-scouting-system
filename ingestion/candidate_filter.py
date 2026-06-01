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

# ── Signal Patterns ───────────────────────────────────────────────────────────

STARTUP_SIGNALS = re.compile(
    r"\b("
    r"startup|start-up|founded|co-founded|founder|"
    r"ceo|cto|coo|cpo|managing\s+director|geschäftsführer|"
    r"raised|funding|funded|investment|investor|venture|"
    r"seed|series\s+[a-c]|pre-seed|post-seed|"
    r"scale-up|scaleup|unicorn|soonicorn|"
    r"accelerat|incubat|portfolio|"
    r"spin-?off|spin-?out|"
    r"pitch|traction|mrr|arr|runway|exit|ipo|"
    r"gmbh|ag\b|ug\b|ltd|inc\b|sas|bv\b|s\.a\."
    r")\b",
    re.IGNORECASE,
)

GEO_SIGNALS = re.compile(
    r"\b("
    r"germany|deutschland|german|"
    r"austria|österreich|austrian|"
    r"switzerland|schweiz|swiss|"
    r"munich|münchen|berlin|hamburg|frankfurt|cologne|köln|"
    r"stuttgart|augsburg|nuremberg|nürnberg|düsseldorf|"
    r"vienna|wien|graz|salzburg|linz|"
    r"zurich|zürich|basel|geneva|genf|bern|"
    r"europe|european|dach|eu\b"
    r")\b",
    re.IGNORECASE,
)

TECH_SIGNALS = re.compile(
    r"\b("
    r"platform|software|saas|paas|api|sdk|"
    r"ai\b|ml\b|llm\b|deep\s*learning|machine\s*learning|"
    r"algorithm|autonomous|automation|"
    r"cloud|data|analytics|dashboard|"
    r"sensor|hardware|robot|drone|iot\b|"
    r"marketplace|fintech|climatetech|proptech|insurtech|"
    r"b2b|b2c|b2g|enterprise|"
    r"product|solution|technology|digital"
    r")\b",
    re.IGNORECASE,
)

COMPANY_SIGNALS = re.compile(
    r"("
    r"GmbH|AG\b|UG\b|Ltd\.?|Inc\.?|S\.A\.S|B\.V\.|"
    r"\bcompan(?:y|ies)\b|\bfirm\b|\benterprise\b|\bventure\b|"
    r"\bteam\b|\bour\s+mission\b|\bwe\s+(?:are|build|help|enable)\b"
    r")",
    re.IGNORECASE,
)

# ── Thresholds ────────────────────────────────────────────────────────────────

MIN_SCORE = 2    # must match at least 2 of 4 signal categories
MIN_WORDS = 25   # skip navigation bars, breadcrumbs, cookie banners


# ── Public API ────────────────────────────────────────────────────────────────

def is_relevant(chunk: str) -> bool:
    """
    Return True if *chunk* is likely to contain startup mentions.

    Scoring (each category contributes at most +1):
      +1  STARTUP_SIGNALS  — funding, founding events, role titles
      +1  GEO_SIGNALS      — European / DACH geographies
      +1  TECH_SIGNALS     — technology and product vocabulary
      +1  COMPANY_SIGNALS  — explicit entity markers

    Chunks with fewer than MIN_WORDS words are always rejected regardless
    of signal score (they are usually UI chrome, not content).
    """
    if len(chunk.split()) < MIN_WORDS:
        return False

    score = 0
    if STARTUP_SIGNALS.search(chunk):
        score += 1
    if GEO_SIGNALS.search(chunk):
        score += 1
    if TECH_SIGNALS.search(chunk):
        score += 1
    if COMPANY_SIGNALS.search(chunk):
        score += 1

    return score >= MIN_SCORE
