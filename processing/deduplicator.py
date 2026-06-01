"""
Startup identity resolution: name normalization, fingerprinting, and
deterministic UUID generation for stable Qdrant point IDs.

The fingerprint = sha256(normalized_name | domain)[:32]
The stable UUID  = uuid5(NAMESPACE_URL, fingerprint)

This means the same startup always maps to the same DB row and the same
Qdrant point, regardless of which source or ingestion run surfaced it.

Phase 2 adds fuzzy_match_existing() as a fallback for cases where the
same company surfaces with a different domain or no website at all.
"""
import re
import uuid
import hashlib
import logging
from urllib.parse import urlparse
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Legal entity suffixes that carry no identity information
_LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|ag|ug|kg|ohg|kgaa|gbr|ev|inc|llc|ltd|bv|sas|sarl|sl|ab|oy|"
    r"as|se|plc|corp|co|srl|spa|nv|sa|pte|pvt)\b",
    re.IGNORECASE,
)

# Punctuation except internal hyphens
_PUNCT = re.compile(r"[^\w\s-]")
_WHITESPACE = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """
    Produce a canonical form of a company name for fuzzy comparison.

    Steps:
      1. Lowercase + strip
      2. Remove legal entity suffixes (GmbH, AG, Inc, Ltd …)
      3. Strip remaining punctuation
      4. Collapse whitespace

    Examples:
      "DeepDrive GmbH"   → "deepdrive"
      "Deep-Drive AG"    → "deep-drive"
      "DeepDrive"        → "deepdrive"
    """
    if not name:
        return ""
    name = name.lower().strip()
    name = _LEGAL_SUFFIXES.sub("", name)
    name = _PUNCT.sub("", name)
    name = _WHITESPACE.sub(" ", name).strip()
    return name


def extract_domain(url: str) -> str:
    """
    Return the bare domain from a URL, stripping 'www.' prefix.

    Examples:
      "https://www.deepdrive.eu/about" → "deepdrive.eu"
      "deepdrive.eu"                   → "deepdrive.eu"
      ""                               → ""
    """
    if not url:
        return ""
    try:
        # Handle bare domains that have no scheme
        if "://" not in url:
            url = f"https://{url}"
        netloc = urlparse(url).netloc.lower()
        return netloc.lstrip("www.")
    except Exception:
        return ""


def generate_fingerprint(name: str, website: str = "") -> str:
    """
    Create a stable 32-char hex identity key for a startup.

    fingerprint = sha256( normalize(name) + "|" + domain )[:32]

    If name is empty, returns an empty string (caller must handle).
    """
    normalized = normalize_company_name(name)
    if not normalized:
        return ""
    domain = extract_domain(website)
    raw = f"{normalized}|{domain}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def name_to_stable_uuid(name: str, website: str = "") -> Optional[str]:
    """
    Derive a deterministic UUID-v5 from a startup's identity.

    Qdrant requires UUIDs as point IDs. This ensures every ingestion of
    the same company produces the same UUID, making Qdrant's upsert truly
    idempotent (update in place, not insert duplicate).

    Returns None if name is empty.
    """
    fingerprint = generate_fingerprint(name, website)
    if not fingerprint:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))


def fuzzy_match_existing(
    name: str,
    db_session,
    threshold: int = 88,
) -> Optional[Tuple[str, str, int]]:
    """
    Find an existing startup in PostgreSQL whose normalized name closely
    matches the given name. This is the Phase 2 fallback for cases where
    the fingerprint lookup misses (different domain, no website, etc.).

    Uses token_sort_ratio which is insensitive to word order and handles
    common variants:
      "DeepDrive Technologies GmbH"  ↔  "DeepDrive Technologies"  → ~93
      "Celonis AG"                   ↔  "Celonis"                  → 100
      "Deep-Drive"                   ↔  "DeepDrive"                → ~91

    Short names (< 4 chars after normalizing) are skipped to avoid
    false positives on generic words like "AI", "Hub", "Lab".

    Returns (id_str, matched_normalized_name, score) or None.
    """
    try:
        from rapidfuzz import process, fuzz
        from database.models import Startup
    except ImportError:
        logger.warning("[Dedup] rapidfuzz not installed — skipping fuzzy match")
        return None

    normalized = normalize_company_name(name)
    if not normalized or len(normalized) < 4:
        return None

    rows = db_session.query(Startup.id, Startup.normalized_name).filter(
        Startup.normalized_name.isnot(None),
        Startup.normalized_name != "",
    ).all()

    if not rows:
        return None

    # Build {id_str: normalized_name} dict; skip short candidates too
    choices = {
        str(row_id): row_name
        for row_id, row_name in rows
        if row_name and len(row_name) >= 4
    }

    if not choices:
        return None

    # extractOne returns (matched_value, score, key) for a dict input
    result = process.extractOne(
        normalized,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold,
    )

    if result:
        matched_name, score, matched_id = result
        return (matched_id, matched_name, int(score))
    return None
