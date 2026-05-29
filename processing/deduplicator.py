"""
Startup identity resolution: name normalization, fingerprinting, and
deterministic UUID generation for stable Qdrant point IDs.

The fingerprint = sha256(normalized_name | domain)[:32]
The stable UUID  = uuid5(NAMESPACE_URL, fingerprint)

This means the same startup always maps to the same DB row and the same
Qdrant point, regardless of which source or ingestion run surfaced it.
"""
import re
import uuid
import hashlib
import logging
from urllib.parse import urlparse
from typing import Optional

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
