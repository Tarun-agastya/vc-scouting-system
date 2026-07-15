"""
Live source registry loader — Phase S (dynamic sources).

Reads config/sources.yaml fresh on every call so adding/removing a source,
newsletter sender, or search keyword takes effect on the NEXT ingestion run
with no restart, no code change, no deploy.

Safety guarantees
------------------
- A single malformed entry (bad field, wrong type) is skipped and logged —
  it does NOT take down the rest of the file or the ingestion run.
- A totally broken file (invalid YAML, missing, wrong root type) falls back
  to the last successfully parsed version held in memory, or an empty
  registry on a cold start with no prior good load. Ingestion never crashes
  because of a bad edit to this file.
- Writes (add/delete) use ruamel.yaml round-trip mode so human comments and
  formatting in sources.yaml survive programmatic edits from the API/dashboard.

Usage
-----
    from config.source_loader import (
        get_rss_feeds, get_web_sources, get_high_priority_sources,
        get_sources_by_type, get_enrichment_sources,
        get_newsletter_senders, get_newsletter_search_terms,
        add_web_source, add_rss_feed, delete_source,
        SourceType, CrawlStrategy, Priority, WebSourceEntry,
    )
"""
from __future__ import annotations

import io
import os
import re
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

import yaml
from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_YAML_PATH = os.path.join(_PROJECT_ROOT, "config", "sources.yaml")

_ruamel = YAML()
_ruamel.preserve_quotes = True
_ruamel.indent(mapping=2, sequence=4, offset=2)
# Prevent ruamel from folding long values (e.g. URLs) onto a continuation
# line — that cosmetic wrapping churns the diff and is fragile to get wrong
# on re-parse. A wide line width effectively disables wrapping.
_ruamel.width = 100_000


# ── Enumerations (unchanged from the original config/source_registry.py) ─────

class SourceType(str, Enum):
    UNIVERSITY_HUB        = "university_hub"
    INCUBATOR             = "incubator"
    ACCELERATOR           = "accelerator"
    STARTUP_NETWORK       = "startup_network"
    INTELLIGENCE_PLATFORM = "intelligence_platform"


class CrawlStrategy(str, Enum):
    WEBSITE_SCRAPE   = "website_scrape"
    NEWSLETTER_PARSE = "newsletter_parse"
    RSS_FEED         = "rss_feed"
    ENRICHMENT_ONLY  = "enrichment_only"  # never a primary scrape target


class Priority(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


# ── Row schemas ────────────────────────────────────────────────────────────────

class RSSFeedEntry(BaseModel):
    name: str
    url: str
    region: str = "europe"
    type: str = "news"


class WebSourceEntry(BaseModel):
    source_id:       str
    source_name:     str
    source_type:     SourceType
    location:        str = "Global"
    primary_url:     str
    crawl_strategy:  CrawlStrategy = CrawlStrategy.WEBSITE_SCRAPE
    crawl_frequency: str = "weekly"
    priority:        Priority = Priority.MEDIUM


class ParsedSources:
    """In-memory snapshot of one successfully parsed sources.yaml load."""

    def __init__(
        self,
        rss_feeds: List[RSSFeedEntry],
        web_sources: List[WebSourceEntry],
        newsletter_senders: List[str],
        newsletter_search_terms: List[str],
    ):
        self.rss_feeds = rss_feeds
        self.web_sources = web_sources
        self.newsletter_senders = newsletter_senders
        self.newsletter_search_terms = newsletter_search_terms


_EMPTY = ParsedSources([], [], [], [])
_last_known_good: Optional[ParsedSources] = None


# ── Row-level parsing (skip-and-log, never fail the whole file) ──────────────

def _parse_rss_feeds(raw_list) -> List[RSSFeedEntry]:
    feeds = []
    for i, item in enumerate(raw_list or []):
        try:
            feeds.append(RSSFeedEntry(**item))
        except (ValidationError, TypeError) as exc:
            logger.error(f"[Sources] Skipping invalid rss_feeds entry #{i} ({item!r}): {exc}")
    return feeds


def _parse_web_sources(raw_list) -> List[WebSourceEntry]:
    sources = []
    seen_ids = set()
    for i, item in enumerate(raw_list or []):
        try:
            entry = WebSourceEntry(**item)
        except (ValidationError, TypeError) as exc:
            logger.error(f"[Sources] Skipping invalid web_sources entry #{i} ({item!r}): {exc}")
            continue
        if entry.source_id in seen_ids:
            logger.error(f"[Sources] Skipping duplicate source_id '{entry.source_id}' (entry #{i})")
            continue
        seen_ids.add(entry.source_id)
        sources.append(entry)
    return sources


# ── Load (hot, called fresh every time) ───────────────────────────────────────

def load_sources() -> ParsedSources:
    """
    Parse config/sources.yaml fresh from disk.

    Never raises. On any file-level problem (missing, invalid YAML, wrong
    root shape) logs the error and returns the last successfully parsed
    snapshot, or an empty registry if there has never been a good load.
    """
    global _last_known_good

    try:
        with open(SOURCES_YAML_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(f"[Sources] {SOURCES_YAML_PATH} not found — using last-known-good registry")
        return _last_known_good or _EMPTY
    except yaml.YAMLError as exc:
        logger.error(f"[Sources] sources.yaml has invalid YAML syntax: {exc} — using last-known-good registry")
        return _last_known_good or _EMPTY

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        logger.error("[Sources] sources.yaml root must be a mapping (key: value) — using last-known-good registry")
        return _last_known_good or _EMPTY

    parsed = ParsedSources(
        rss_feeds=_parse_rss_feeds(raw.get("rss_feeds")),
        web_sources=_parse_web_sources(raw.get("web_sources")),
        newsletter_senders=[str(s) for s in (raw.get("newsletter_senders") or [])],
        newsletter_search_terms=[str(s) for s in (raw.get("newsletter_search_terms") or [])],
    )
    _last_known_good = parsed
    return parsed


# ── Read accessors (match the old config/source_registry.py interface) ───────

def get_rss_feeds() -> List[dict]:
    """RSS feeds as plain dicts — same shape as the old hardcoded RSS_FEEDS."""
    return [f.model_dump() for f in load_sources().rss_feeds]


def get_web_sources() -> List[WebSourceEntry]:
    return load_sources().web_sources


def get_high_priority_sources() -> List[WebSourceEntry]:
    return [s for s in get_web_sources() if s.priority == Priority.HIGH]


def get_sources_by_type(source_type: SourceType) -> List[WebSourceEntry]:
    return [s for s in get_web_sources() if s.source_type == source_type]


def get_enrichment_sources() -> List[WebSourceEntry]:
    return [s for s in get_web_sources() if s.crawl_strategy == CrawlStrategy.ENRICHMENT_ONLY]


def get_newsletter_senders() -> List[str]:
    return load_sources().newsletter_senders


def get_newsletter_search_terms() -> List[str]:
    return load_sources().newsletter_search_terms


# ── Writes (comment-preserving round-trip via ruamel.yaml) ───────────────────

# Matches only the auto-generated attribution stamp we add in
# _attributed_entry() — never a human-authored comment. Used to clean up an
# orphaned stamp left behind when its entry is later removed via delete_source.
_ATTRIBUTION_STAMP_RE = re.compile(r"^\s*# Added via dashboard on \d{4}-\d{2}-\d{2}\s*$")


def _strip_orphaned_attribution_stamps(text: str) -> str:
    """
    Drop any "# Added via dashboard on <date>" line that no longer sits
    directly above a list item — i.e. its entry was deleted. Only ever
    touches lines matching our own auto-generated format; every
    human-written comment is left completely untouched.
    """
    lines = text.split("\n")
    kept = []
    for i, line in enumerate(lines):
        if _ATTRIBUTION_STAMP_RE.match(line):
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if re.match(r"^\s*-\s", next_line):
                kept.append(line)  # still attached to a surviving entry
            continue  # orphaned — drop it
        kept.append(line)
    return "\n".join(kept)


def _read_document():
    """Load the YAML file with ruamel so comments/formatting round-trip."""
    if not os.path.exists(SOURCES_YAML_PATH):
        raise FileNotFoundError(f"{SOURCES_YAML_PATH} does not exist")
    with open(SOURCES_YAML_PATH, "r", encoding="utf-8") as f:
        doc = _ruamel.load(f)
    if doc is None:
        doc = {}
    return doc


def _write_document(doc) -> None:
    buf = io.StringIO()
    _ruamel.dump(doc, buf)
    text = _strip_orphaned_attribution_stamps(buf.getvalue())
    with open(SOURCES_YAML_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def _attributed_entry(fields: dict) -> CommentedMap:
    """
    Build a ruamel CommentedMap with a "Added via dashboard" start-comment so
    a newly appended entry is self-describing — this doesn't depend on any
    pre-existing section comment (which can visually drift to sit above a
    newly appended item; see _write_document / add_web_source).
    """
    entry = CommentedMap(fields)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry.yaml_set_start_comment(f"Added via dashboard on {stamp}")
    return entry


def add_rss_feed(entry: dict) -> RSSFeedEntry:
    """Validate and append a new RSS feed. Raises ValueError on invalid input."""
    try:
        validated = RSSFeedEntry(**entry)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    doc = _read_document()
    doc.setdefault("rss_feeds", [])
    doc["rss_feeds"].append(_attributed_entry(validated.model_dump()))
    _write_document(doc)
    logger.info(f"[Sources] Added RSS feed '{validated.name}' ({validated.url})")
    return validated


def add_web_source(entry: dict) -> WebSourceEntry:
    """Validate and append a new web source. Raises ValueError on invalid input or duplicate id."""
    try:
        validated = WebSourceEntry(**entry)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    existing_ids = {s.source_id for s in get_web_sources()}
    if validated.source_id in existing_ids:
        raise ValueError(f"source_id '{validated.source_id}' already exists")

    doc = _read_document()
    doc.setdefault("web_sources", [])
    # model_dump(mode="json") renders enums as their plain string values
    doc["web_sources"].append(_attributed_entry(validated.model_dump(mode="json")))
    _write_document(doc)
    logger.info(f"[Sources] Added web source '{validated.source_name}' ({validated.source_id})")
    return validated


def delete_source(source_id: str) -> bool:
    """Remove a web_sources entry by source_id. Returns True if found and removed."""
    doc = _read_document()
    sources = doc.get("web_sources") or []
    remaining = [s for s in sources if s.get("source_id") != source_id]

    if len(remaining) == len(sources):
        return False

    doc["web_sources"] = remaining
    _write_document(doc)
    logger.info(f"[Sources] Deleted web source '{source_id}'")
    return True
