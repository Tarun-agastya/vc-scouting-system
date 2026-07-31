"""
PageStrategy — the decision object that replaces per-website tuning.

A leaf module on purpose: it imports nothing from the pipeline, so
web_scraper, chunker, worker_queue and site_inspector can all depend on it
without a cycle. (That cycle is exactly why ingestion/chunker.py duplicates
the logo-grid marker string instead of importing it — see its comment.)

Today the fetch stage tells the chunk stage what kind of page it saw by
embedding a magic string in the page text. This dataclass is the real channel
that replaces it: one explicit, inspectable object carried on PageItem and
ChunkItem, produced either by the deterministic inspector or the cached
strategist, and falling back to DEFAULT — which reproduces today's behaviour
byte-for-byte — whenever nothing is known.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from typing import Optional

# Vocabularies. Kept here (not in the LLM schema module) so the deterministic
# path and the strategist path can never drift apart.
PAGE_SHAPES = (
    "logo_grid",       # repeating name-only cards: a portfolio wall
    "card_directory",  # repeating cards carrying real text per entity
    "detail_page",     # one entity, described in depth
    "prose_listing",   # entities mentioned in running text
    "article_feed",    # news/events/blog list — NOT an entity directory
    "mixed",
    "non_content",     # nav, legal, contact — nothing to extract
    "unknown",
)
TEXT_MODES = ("full_text", "main_prose", "alt_harvest", "card_structured")
CHUNK_MODES = ("sliding_window", "name_batch", "per_card", "blurb")

# Shapes that describe companies. Only these carry an entity expectation, so
# an events list can never manufacture a recall shortfall the extractor is
# right to leave unfilled.
ENTITY_SHAPES = frozenset({"logo_grid", "card_directory", "detail_page"})


@dataclass(frozen=True)
class PageStrategy:
    page_shape: str = "unknown"
    text_extraction: str = "full_text"
    chunking: str = "sliding_window"
    needs_render: bool = False
    paginate: bool = False
    follow_detail_links: bool = False
    detail_link_pattern: Optional[str] = None
    bypass_candidate_filter: bool = False
    names_per_chunk: Optional[int] = None
    load_more_selector: Optional[str] = None
    max_pages: Optional[int] = None
    max_depth: Optional[int] = None
    max_load_more: Optional[int] = None
    expected_entity_count: int = 0
    profile_id: Optional[str] = None
    confidence: str = "low"
    reason: str = ""
    # llm | deterministic | llm_overridden | learned | pinned | default
    source: str = "default"

    @property
    def expects_entities(self) -> bool:
        """Whether a recall shortfall is even meaningful for this page."""
        return self.page_shape in ENTITY_SHAPES and self.expected_entity_count > 0

    def with_(self, **kw) -> "PageStrategy":
        """Frozen-safe copy, for the R-5 retry ladder."""
        return replace(self, **kw)

    def to_dict(self) -> dict:
        return asdict(self)


# Reproduces today's pipeline exactly: plain BeautifulSoup text, sliding-window
# chunks, heuristic filter applied, no render, no pagination. Anything unknown
# resolves here, so an unprofiled source behaves precisely as it does now.
PageStrategy.DEFAULT = PageStrategy()
