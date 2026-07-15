"""
DACH Startup Intelligence Platform — Source Registry (Phase S: thin shim)

The actual source data now lives in config/sources.yaml, loaded and
validated by config/source_loader.py. This module exists only to keep the
original import interface working unchanged for every caller
(processing/scout_controller.py, scripts/run_ingestion.py):

    from config.source_registry import (
        SOURCE_REGISTRY, get_high_priority_sources, get_sources_by_type,
        get_enrichment_sources, SourceType, Priority, CrawlStrategy,
    )

SOURCE_REGISTRY is a lazy proxy — each `for s in SOURCE_REGISTRY:` call
re-reads config/sources.yaml fresh, so adding/removing a source there takes
effect on the next ingestion run with no restart needed.

Adding a new source means one new entry in config/sources.yaml — no code
in this file or any caller needs to change.
"""
from __future__ import annotations

from typing import Iterator, List

from config.source_loader import (
    CrawlStrategy,
    Priority,
    SourceType,
    WebSourceEntry as SourceDefinition,
    get_enrichment_sources,
    get_high_priority_sources,
    get_sources_by_type,
    get_web_sources,
)

__all__ = [
    "SourceType",
    "CrawlStrategy",
    "Priority",
    "SourceDefinition",
    "SOURCE_REGISTRY",
    "get_high_priority_sources",
    "get_sources_by_type",
    "get_enrichment_sources",
]


class _LiveSourceRegistry:
    """
    Iterable proxy standing in for the old static SOURCE_REGISTRY list.

    Every iteration re-reads config/sources.yaml via the loader, so this
    object never goes stale even though it's imported once at module load.
    """

    def __iter__(self) -> Iterator[SourceDefinition]:
        return iter(get_web_sources())

    def __len__(self) -> int:
        return len(get_web_sources())

    def __getitem__(self, index):
        return get_web_sources()[index]

    def __repr__(self) -> str:
        return f"<LiveSourceRegistry: {len(self)} sources from config/sources.yaml>"


SOURCE_REGISTRY: List[SourceDefinition] = _LiveSourceRegistry()  # type: ignore[assignment]
