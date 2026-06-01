"""
DACH Startup Intelligence Platform — Source Registry

Single source of truth for every intelligence source this platform monitors.
Ingestion pipelines should loop through this registry rather than hardcoding
URLs or metadata in individual modules.

Design principles
-----------------
* Sources are immutable frozen dataclasses — no accidental mutation at runtime.
* LinkedIn, Crunchbase, and Tracxn are CrawlStrategy.ENRICHMENT_ONLY.
  They must NEVER be used as primary scraping targets.
* Priority controls ingestion order: HIGH → MEDIUM → LOW.
* Adding a new source means one new SourceDefinition entry here; no other
  file needs to change.

Usage
-----
    from config.source_registry import (
        get_high_priority_sources,
        get_sources_by_type,
        get_enrichment_sources,
        SourceType,
        Priority,
    )

    for source in get_high_priority_sources():
        print(source.source_id, source.primary_url)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List


# ── Enumerations ──────────────────────────────────────────────────────────────

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


# ── Source Definition ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceDefinition:
    """Immutable descriptor for a single intelligence source."""
    source_id:        str             # snake_case unique key
    source_name:      str             # human-readable display name
    source_type:      SourceType
    location:         str             # city / region / "Global"
    primary_url:      str
    crawl_strategy:   CrawlStrategy
    crawl_frequency:  str             # "weekly" | "biweekly" | "on_demand"
    priority:         Priority


# ── Registry ──────────────────────────────────────────────────────────────────
# Ordered HIGH → MEDIUM → LOW so a simple iteration naturally respects priority.

SOURCE_REGISTRY: List[SourceDefinition] = [

    # =========================================================================
    # HIGH PRIORITY — scrape weekly, process first
    # =========================================================================

    SourceDefinition(
        source_id       = "starthub_augsburg",
        source_name     = "StartHub Augsburg",
        source_type     = SourceType.UNIVERSITY_HUB,
        location        = "Augsburg, Germany",
        primary_url     = "https://www.uni-augsburg.de/de/organisation/einrichtungen/starthub/startseite/",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "dzs_schwaben",
        source_name     = "D.Z.S. Digitales Zentrum Schwaben",
        source_type     = SourceType.INCUBATOR,
        location        = "Augsburg, Germany",
        primary_url     = "https://schwaben.digital",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "tha_funkenwerk",
        source_name     = "THA Funkenwerk",
        source_type     = SourceType.UNIVERSITY_HUB,
        location        = "Augsburg, Germany",
        primary_url     = "https://funkenwerk.tha.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "hbc_biberach",
        source_name     = "HBC Biberach Gründungsberatung",
        source_type     = SourceType.UNIVERSITY_HUB,
        location        = "Biberach an der Riß, Germany",
        primary_url     = "https://www.hochschule-biberach.de/forschung-transfer/gruendung",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "allgaeu_digital",
        source_name     = "Allgäu Digital",
        source_type     = SourceType.STARTUP_NETWORK,
        location        = "Allgäu, Germany",
        primary_url     = "https://allgaeu-digital.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "xpreneurs",
        source_name     = "XPRENEURS",
        source_type     = SourceType.ACCELERATOR,
        location        = "Munich, Germany",
        primary_url     = "https://www.xpreneurs.io",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "techfounders",
        source_name     = "TechFounders",
        source_type     = SourceType.ACCELERATOR,
        location        = "Munich, Germany",
        primary_url     = "https://www.techfounders.com",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "strascheg_center",
        source_name     = "Strascheg Center for Entrepreneurship",
        source_type     = SourceType.INCUBATOR,
        location        = "Munich, Germany",
        primary_url     = "https://sce.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "cdtm",
        source_name     = "CDTM — Center for Digital Technology and Management",
        source_type     = SourceType.UNIVERSITY_HUB,
        location        = "Munich, Germany",
        primary_url     = "https://www.cdtm.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "kit_gruenderschmiede",
        source_name     = "KIT Gründerschmiede",
        source_type     = SourceType.UNIVERSITY_HUB,
        location        = "Karlsruhe, Germany",
        primary_url     = "https://www.gruenderschmiede.kit.edu",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "cyberlab_karlsruhe",
        source_name     = "CyberLab Karlsruhe",
        source_type     = SourceType.INCUBATOR,
        location        = "Karlsruhe, Germany",
        primary_url     = "https://www.cyberlab.eu",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "campus_founders",
        source_name     = "Campus Founders",
        source_type     = SourceType.ACCELERATOR,
        location        = "Heilbronn, Germany",
        primary_url     = "https://www.campus-founders.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    SourceDefinition(
        source_id       = "munich_startup",
        source_name     = "Munich Startup",
        source_type     = SourceType.STARTUP_NETWORK,
        location        = "Munich, Germany",
        primary_url     = "https://www.munich-startup.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "weekly",
        priority        = Priority.HIGH,
    ),

    # =========================================================================
    # MEDIUM PRIORITY — scrape biweekly
    # =========================================================================

    SourceDefinition(
        source_id       = "baystartup",
        source_name     = "BayStartup",
        source_type     = SourceType.STARTUP_NETWORK,
        location        = "Munich, Germany",
        primary_url     = "https://www.baystartup.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "biweekly",
        priority        = Priority.MEDIUM,
    ),

    SourceDefinition(
        source_id       = "startup_autobahn",
        source_name     = "Startup Autobahn",
        source_type     = SourceType.ACCELERATOR,
        location        = "Stuttgart, Germany",
        primary_url     = "https://www.startup-autobahn.com",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "biweekly",
        priority        = Priority.MEDIUM,
    ),

    SourceDefinition(
        source_id       = "arena2036",
        source_name     = "Arena2036",
        source_type     = SourceType.INCUBATOR,
        location        = "Stuttgart, Germany",
        primary_url     = "https://www.arena2036.de",
        crawl_strategy  = CrawlStrategy.WEBSITE_SCRAPE,
        crawl_frequency = "biweekly",
        priority        = Priority.MEDIUM,
    ),

    # =========================================================================
    # LOW PRIORITY — enrichment only, never primary scrape targets
    # =========================================================================

    SourceDefinition(
        source_id       = "linkedin",
        source_name     = "LinkedIn",
        source_type     = SourceType.INTELLIGENCE_PLATFORM,
        location        = "Global",
        primary_url     = "https://www.linkedin.com",
        crawl_strategy  = CrawlStrategy.ENRICHMENT_ONLY,
        crawl_frequency = "on_demand",
        priority        = Priority.LOW,
    ),

    SourceDefinition(
        source_id       = "crunchbase",
        source_name     = "Crunchbase",
        source_type     = SourceType.INTELLIGENCE_PLATFORM,
        location        = "Global",
        primary_url     = "https://www.crunchbase.com",
        crawl_strategy  = CrawlStrategy.ENRICHMENT_ONLY,
        crawl_frequency = "on_demand",
        priority        = Priority.LOW,
    ),

    SourceDefinition(
        source_id       = "tracxn",
        source_name     = "Tracxn",
        source_type     = SourceType.INTELLIGENCE_PLATFORM,
        location        = "Global",
        primary_url     = "https://tracxn.com",
        crawl_strategy  = CrawlStrategy.ENRICHMENT_ONLY,
        crawl_frequency = "on_demand",
        priority        = Priority.LOW,
    ),
]


# ── Helper Functions ──────────────────────────────────────────────────────────

def get_high_priority_sources() -> List[SourceDefinition]:
    """
    Return all sources with Priority.HIGH.

    The registry is already ordered HIGH → MEDIUM → LOW, so the result
    is naturally sorted for pipeline use without additional sorting.
    """
    return [s for s in SOURCE_REGISTRY if s.priority == Priority.HIGH]


def get_sources_by_type(source_type: SourceType) -> List[SourceDefinition]:
    """
    Return all sources matching the given SourceType.

    Example
    -------
        accelerators = get_sources_by_type(SourceType.ACCELERATOR)
    """
    return [s for s in SOURCE_REGISTRY if s.source_type == source_type]


def get_enrichment_sources() -> List[SourceDefinition]:
    """
    Return sources whose crawl_strategy is ENRICHMENT_ONLY.

    These must never be treated as primary scraping targets.
    They are called only to enrich an already-discovered startup record
    with additional metadata (funding data, employee count, LinkedIn URL, etc.).
    """
    return [s for s in SOURCE_REGISTRY if s.crawl_strategy == CrawlStrategy.ENRICHMENT_ONLY]
