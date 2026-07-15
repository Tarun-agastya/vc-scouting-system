"""
All startup intelligence sources: RSS feeds, accelerators,
incubators, university spinoff pages, and hubs.
Strictly focused on the DACH region and Europe.

Phase S: RSS_FEEDS now lives in config/sources.yaml (loaded/validated by
config/source_loader.py) instead of being hardcoded here. RSS_FEEDS below is
a lazy proxy — every access re-reads the YAML file fresh, so adding/removing
a feed there takes effect on the next ingestion run with no restart needed.

ACCELERATOR_SOURCES / UNIVERSITY_SOURCES / HUB_SOURCES were legacy/unused
duplicates of what's now config/sources.yaml's web_sources section (the
live registry powering scout_controller's accelerator/university runs) —
removed to avoid two disagreeing lists of the same sources.
"""
from typing import Iterator, List

from config.source_loader import get_rss_feeds


class _LiveRSSFeeds:
    """
    Iterable proxy standing in for the old static RSS_FEEDS list.

    Every iteration re-reads config/sources.yaml via the loader, so this
    stays fresh even though it's imported once at module load time.
    """

    def __iter__(self) -> Iterator[dict]:
        return iter(get_rss_feeds())

    def __len__(self) -> int:
        return len(get_rss_feeds())

    def __getitem__(self, index):
        return get_rss_feeds()[index]

    def __repr__(self) -> str:
        return f"<LiveRSSFeeds: {len(self)} feeds from config/sources.yaml>"


RSS_FEEDS: List[dict] = _LiveRSSFeeds()  # type: ignore[assignment]

# ── Default Search Prompts for Sector Intelligence ────────────────────────────
# Not sourced from sources.yaml (these are query templates, not sources) —
# kept here as-is.
SECTOR_PROMPTS = {
    "ai": "AI machine learning deep learning neural networks LLM artificial intelligence",
    "fintech": "fintech financial technology payments banking neobank insurtech",
    "healthtech": "healthtech medtech digital health biotech genomics diagnostics",
    "climatetech": "climate tech cleantech green energy sustainability carbon",
    "deeptech": "deep tech hardware robotics quantum computing semiconductors",
    "saas": "SaaS software B2B enterprise cloud platform workflow automation",
    "ecommerce": "ecommerce marketplace D2C retail commerce logistics",
}
