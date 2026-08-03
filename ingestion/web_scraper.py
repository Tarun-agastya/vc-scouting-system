import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Shared browser-like headers to reduce bot-detection false positives
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Below this many extracted characters, a "successful" (HTTP 200) static
# fetch is treated as an empty JS-rendering shell rather than a real page —
# React/Vue/etc. sites often return a 200 with a near-empty <body> and load
# all content client-side, which a plain httpx GET can never see.
_MIN_STATIC_TEXT_LEN = 200

# How many surviving alt entries a page needs before its alt block is treated
# as a curated portfolio/logo grid rather than incidental imagery. See the
# use site in _extract_text for the incident that motivated it.
_MIN_LOGO_GRID_ENTRIES = 12

# Button/link text that reveals more of a paginated directory list. Matched
# case-insensitively as a substring by Playwright's :has-text(). Bilingual
# (DE/EN) since most sources here are German. Ordered most- to least-specific.
_LOAD_MORE_PATTERNS = (
    "Mehr laden", "Mehr anzeigen", "Weitere laden", "Mehr Ergebnisse",
    "Alle anzeigen", "Load more", "Show more", "See more", "View more",
)

# Cookie-consent accept buttons. Some sites render their content grid behind
# (or with clicks blocked by) a consent overlay until it's dismissed —
# confirmed live 24 Jul: zollhof.de's portfolio grid didn't visibly change
# without this, though the real gap there was the alt-text issue above; still
# cheap, safe, and worth doing before reading/paginating ANY rendered page.
_COOKIE_ACCEPT_PATTERNS = (
    "Allow and continue", "Accept all", "Accept All", "Accept",
    "Alle akzeptieren", "Akzeptieren", "Zustimmen", "Ich stimme zu", "OK",
)

# URL path segments that mark a page as high-value startup content (a
# portfolio, a company/startup profile, an alumni/cohort list) so the crawl
# frontier visits them BEFORE generic section/nav pages within its page
# budget. Matched at the path-segment level, like SKIP_PATTERNS.
_PRIORITY_PATTERNS: frozenset = frozenset({
    "startup", "startups", "portfolio", "portfolios", "company", "companies",
    "unternehmen", "founders", "gruender", "alumni", "batch", "cohort",
    "ventures", "scaleup", "scaleups", "incubation", "members", "member",
})


def _url_priority(url: str) -> int:
    """0 = high-value (startup/portfolio/company page), 1 = everything else."""
    path_parts = urlparse(url).path.lower().strip("/").split("/")
    return 0 if any(p in _PRIORITY_PATTERNS for p in path_parts if p) else 1


# ── URL Skip Patterns ─────────────────────────────────────────────────────────
# Path segments that indicate pages with no startup intelligence value.
# Checked at the segment level so '/login' is skipped but '/online-platform'
# and '/innovation' are not false-positives.
# Add entries here to expand coverage without touching any other code.
SKIP_PATTERNS: frozenset = frozenset({
    # Authentication / account management
    "login", "logout", "signin", "signup", "register",
    "auth", "oauth", "sso", "password", "reset-password", "forgot-password",
    # Administrative
    "admin", "intranet", "dashboard", "backend", "cms",
    # Recruitment  (irrelevant to startup discovery)
    "jobs", "karriere", "career", "careers",
    "stellenangebote", "stellenangebot", "bewerbung", "apply", "hiring",
    # Legal / compliance
    "privacy", "datenschutz", "impressum", "legal",
    "terms", "agb", "cookie", "cookies", "gdpr",
    # Contact / generic navigation
    "contact", "kontakt", "support", "help", "faq",
    "newsletter", "subscribe", "unsubscribe",
    # Press / media  (usually about the org, not startups)
    "press", "media",
    # User account areas
    "profile", "account", "settings", "preferences",
    # Utility
    "search", "sitemap",
})


# ── URL Utilities ─────────────────────────────────────────────────────────────

def _base_domain(url: str) -> str:
    """Return the lowercase netloc of a URL (used for domain-isolation checks)."""
    return urlparse(url).netloc.lower()


# Generic UI/icon alt text to drop when harvesting <img alt="..."> names —
# exact match only (not substring), so a real company name that happens to
# contain one of these as part of a longer phrase is never dropped.
_ALT_NOISE = frozenset({
    "logo", "icon", "arrow", "close", "menu", "search", "chevron", "banner",
    "facebook", "twitter", "instagram", "linkedin", "youtube", "tiktok",
    "avatar", "placeholder", "background", "hero", "ok",
})

# Structural junk in alt text — CMS-generated image names, not company names.
# Added 31 Jul after a schwaben.digital run wrote 72 records of which ~50 were
# image filenames ("Medium_20250128-raum-orange-0003", "Xlarge_2023",
# "Article_20191505-dzs-rf-zuschnitt", "Handshake-simple-solid"), because the
# alt harvest passed anything not in the exact-match set above straight
# through as a "portfolio / logo grid entry". Zollhof's grid is genuinely
# company names; most sites' alt text is not, so the harvest needs real
# structure checks, not just a wordlist.
_ALT_JUNK_RE = re.compile(
    r"""
      \.(?:jpe?g|png|svg|webp|gif|avif)$          # ends in an image extension
    | ^(?:x?large|medium|small|thumb|article|img|image|foto|photo|bild)[_-]
                                                  # CMS size/type prefix
    | \b20\d{6}\b                                 # embedded yyyymmdd datestamp
    | [_-]\d{3,}$                                 # trailing sequence: -0001, _074
    | ^\d{6,}                                     # starts with a long digit run
    | _kopie                                      # "copy" suffix
    | \blogo\b                                    # "MIT logo", "Zollhof tech logo" —
                                                  # a caption ON a logo image, not the
                                                  # entity's own name. _ALT_NOISE only
                                                  # exact-matched bare "logo"; this catches
                                                  # the far more common "<Name> logo"
                                                  # convention (found live 3 Aug: cdtm.de's
                                                  # partner-university strip — "MIT logo",
                                                  # "harvard logo" — passed through whole).
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A slug, not a name: 3+ separator-delimited tokens and no spaces
# ("dzs-teamfotos-webseite-jakob", "website-besser-starten",
# "polygon-zuschnitte-team_sk"). Real company names in this position are
# either single tokens, contain spaces, or use at most one separator
# ("e-laborate", "bill.less", "WeSort.AI GmbH") — so requiring 3+ tokens
# keeps those safe.
_ALT_SLUG_RE = re.compile(r"^[^\s]+(?:[-_][^\s]+){2,}$")


def _is_junk_alt(alt: str) -> bool:
    """True if this <img alt> is CMS/structural noise rather than a name."""
    key = alt.strip().lower()
    if len(alt.strip()) < 2 or key in _ALT_NOISE:
        return True
    if _ALT_JUNK_RE.search(alt):
        return True
    if _ALT_SLUG_RE.match(alt):
        return True
    return False


def _extract_text(html: str) -> str:
    """
    Strip boilerplate tags and return clean plain text from raw HTML.

    Also harvests meaningful <img alt="..."> values as a separate trailing
    block. Portfolio/logo-grid pages often show each company as ONLY a logo
    image, with the name living in alt text and NEVER appearing as visible
    text — confirmed live 24 Jul on zollhof.de's startup portfolio page:
    120 company names existed solely as img alt text; plain get_text()
    returned page chrome (category filter chips, "New!" badges) and missed
    every single one. Noise (generic icon/social alt text) is dropped by
    exact match; real company names always pass through untouched.
    """
    soup = BeautifulSoup(html, "html.parser")

    alts: list = []
    seen_alt: set = set()
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        key = alt.lower()
        if key in seen_alt or _is_junk_alt(alt):
            continue
        seen_alt.add(key)
        alts.append(alt)

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)

    # Only claim "this is a portfolio/logo grid" when the page actually looks
    # like one. Below the threshold the alt values are almost certainly
    # incidental page imagery (a couple of partner logos, a team photo), and
    # labelling them as a curated company list tells the extractor to treat
    # each as a real startup — which is exactly how a schwaben.digital run
    # turned staff photos and sponsor logos into 72 records (31 Jul). Genuine
    # grids clear this easily: zollhof.de yields ~120.
    if len(alts) >= _MIN_LOGO_GRID_ENTRIES:
        text += "\n\nPortfolio / logo grid entries on this page:\n" + "\n".join(alts)

    return text


@dataclass
class PageContent:
    """
    Strategy-shaped page content — what extract_content() produces instead of
    a plain string. `text` is what a prose-style chunker splits; `entity_names`
    /`entity_blocks` are structured lists the R-4 chunker dispatches on
    directly for name_batch/per_card chunking, replacing the old approach of
    embedding a marker string in `text` and re-parsing it back out downstream.
    """
    text: str = ""
    entity_names: list = field(default_factory=list)
    entity_blocks: list = field(default_factory=list)  # [(name_or_None, text), ...]
    structural_count: int = 0


def extract_content(html: str, url: str, strategy=None) -> "PageContent":
    """
    Strategy-driven page content extraction (Phase R-4).

    mode == "full_text" (PageStrategy.DEFAULT, or any strategy this function
    doesn't recognise) reproduces _extract_text()'s exact output — so a page
    with no profile, or the adaptive pipeline switched off, is never worse
    off than before this function existed.

    "main_prose" uses trafilatura (already a dependency, already used by
    ingestion/rss_parser.py) to strip boilerplate more aggressively than
    plain BeautifulSoup get_text — for prose-shaped pages where nav/footer
    noise dilutes the real content. Falls back to _extract_text on empty
    output (some JS-rendered pages defeat trafilatura entirely).

    "alt_harvest" / "card_structured" read the primary structural group
    directly via site_inspector.probe_html — deliberately NOT the
    boilerplate-stripped soup _extract_text builds, so a card group nested
    inside a <header>/<nav> wrapper is never lost to an unconditional
    decompose() (the evidence-based-stripping fix the plan calls for falls
    out for free here: these modes never run the decompose step at all).
    If the page no longer structurally confirms the shape a cached profile
    expects (a site redesign), this degrades to full_text rather than
    silently extracting nothing.
    """
    mode = strategy.text_extraction if strategy is not None else "full_text"

    if mode in ("alt_harvest", "card_structured"):
        from ingestion.strategy import ENTITY_SHAPES
        if strategy is None or strategy.page_shape not in ENTITY_SHAPES:
            # A page_shape verdict of non_content/article_feed/etc. means
            # THIS page has nothing worth structurally extracting — even if
            # text_extraction still names a structural mode (carried over
            # from an LLM/deterministic strategy computed for a DIFFERENT
            # pattern's benefit, or simply stale). Found live 3 Aug on
            # zollhof.de's own homepage: its domain-default profile is
            # page_shape="non_content" (correctly: 5 CTA/heading cards, not
            # companies — R-3's own adjudication reason literally says so)
            # but text_extraction="card_structured" — without this gate,
            # those 5 CTA cards were extracted anyway ("Bring me back to the
            # incubation program" ending up as a stored "startup"). Fall
            # back to full_text rather than trust a structural mode this
            # page's own shape verdict disclaims.
            mode = "full_text"

    if mode == "main_prose":
        prose = None
        try:
            import trafilatura
            prose = trafilatura.extract(html, include_comments=False, include_tables=False)
        except Exception as exc:
            logger.debug(f"[Scraper] trafilatura failed for {url}: {exc}")
        if prose and prose.strip():
            return PageContent(text=prose.strip())
        return PageContent(text=_extract_text(html))

    if mode in ("alt_harvest", "card_structured"):
        from ingestion.site_inspector import probe_html
        from config.tuning_loader import get_inspector_config

        sig = probe_html(html, url, get_inspector_config())
        g = sig.primary_group
        if g is None:
            return PageContent(text=_extract_text(html))
        names = g.names()
        if mode == "alt_harvest":
            return PageContent(text="", entity_names=names, structural_count=g.n)
        blocks = [(i.name, i.text) for i in g.items if i.text]
        return PageContent(text="", entity_names=names, entity_blocks=blocks, structural_count=g.n)

    return PageContent(text=_extract_text(html))


def _extract_links(html: str, base_url: str) -> list:
    """Return a deduplicated list of absolute URLs from all <a href> tags."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    links: list = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        # Skip non-navigable schemes
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        # Normalise: drop fragment so #section variants aren't re-visited
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
    return links


def _is_irrelevant_url(url: str) -> bool:
    """
    Return True if any path segment of *url* matches a SKIP_PATTERNS entry.

    Segment-level matching prevents false positives:
      /login           → skipped   (segment 'login' is in SKIP_PATTERNS)
      /online-platform → kept      ('online-platform' is not in SKIP_PATTERNS)
      /portfolio/founders → kept   (neither segment matches)
    """
    path_parts = urlparse(url).path.lower().strip("/").split("/")
    return any(part in SKIP_PATTERNS for part in path_parts if part)


# ── Scraper ───────────────────────────────────────────────────────────────────

class WebScraper:
    """
    Scrapes startup source pages (accelerators, incubators, university hubs).

    `scrape_source` performs an async BFS deep-crawl bounded by `max_depth`
    and `max_pages`, aggregates the text of every visited page into one context
    block, then forwards it to Qwen for startup extraction.
    """

    def __init__(self):
        self._http_timeout = httpx.Timeout(15.0, connect=5.0)

    async def scrape_source(
        self,
        url: str,
        source_type: str = "general",
        max_depth: Optional[int] = None,
        max_pages: Optional[int] = None,
        *,
        force_render: bool = False,
        validation_session=None,
        metrics=None,
        # url_priority_map: dict = None  # extension point: future priority crawling
    ):
        """
        Coordinator: launches the 4-stage worker pipeline and awaits completion.

        Stages
        ------
        Crawler Task  →  page_queue  →  Chunker Task  →  chunk_queue
          →  Qwen Worker(s)  →  storage_queue  →  Storage Worker

        Scraping and Qwen extraction run concurrently.  Back-pressure from
        bounded queues prevents memory explosions when Qwen falls behind.

        metrics : optional pre-created PipelineMetrics. Pass one in when a
          caller (e.g. ScoutController, for live dashboard progress) needs to
          read counters WHILE the run is in flight, not just after it returns.
          When omitted, a fresh one is created — unchanged default behaviour.

        Returns PipelineMetrics for the completed run (callers may ignore it).
        """
        import time
        from config import settings
        from ingestion.worker_queue import (
            PipelineMetrics,
            chunker_task,
            qwen_worker_task,
            storage_worker_task,
        )

        metrics = metrics if metrics is not None else PipelineMetrics()
        # Fall back to the configured crawl reach when the caller doesn't
        # override (the validation harness passes explicit values).
        if max_depth is None:
            max_depth = settings.crawl_max_depth
        if max_pages is None:
            max_pages = settings.crawl_max_pages
        t0 = time.time()
        num_workers = settings.max_qwen_workers

        page_queue    = asyncio.Queue(maxsize=settings.page_queue_size)
        chunk_queue   = asyncio.Queue(maxsize=settings.chunk_queue_size)
        storage_queue = asyncio.Queue(maxsize=settings.storage_queue_size)

        await asyncio.gather(
            self._crawler_task(
                url, source_type, max_depth, max_pages, page_queue, metrics,
                force_render=force_render,
            ),
            chunker_task(page_queue, chunk_queue, metrics),
            *[
                qwen_worker_task(
                    chunk_queue, storage_queue, metrics, i,
                    validation_session=validation_session,
                )
                for i in range(num_workers)
            ],
            storage_worker_task(
                storage_queue, metrics, num_workers,
                validation_session=validation_session,
            ),
        )

        # Phase B (Phase R-5) — recall audit + bounded auto-retry on the
        # worst shortfall pages from the Phase A crawl above. Only ever has
        # anything to do under the adaptive pipeline: metrics.record_expectation
        # is only ever called from the adaptive branch of _crawler_task, so
        # shortfall_pages() is always empty otherwise.
        if settings.adaptive_pipeline_enabled:
            await self._recall_audit_and_retry(
                url, source_type, metrics, validation_session=validation_session,
            )

        metrics.total_processing_time = time.time() - t0
        metrics.report(url)
        return metrics

    # ── Phase B: Recall audit + auto-retry (Phase R-5) ──────────────────────────

    async def _recall_audit_and_retry(
        self, source_url: str, source_type: str, metrics: "PipelineMetrics",
        *, validation_session=None,
    ) -> None:
        """
        Re-run the worst recall-shortfall pages from Phase A through the
        SAME chunker/worker/storage stages, one deterministic retry-ladder
        step at a time (processing.site_profile_store.next_retry_step).

        Bounded two ways: only the recall_retry_max_pages worst offenders are
        retried at all, and recall_retry_max_calls caps the TOTAL added Qwen
        calls across every retry combined — a bad retry (e.g. a page that
        renders into hundreds of chunks) can't silently double a run's cost.

        On a successful retry (recall no longer counts as a shortfall) the
        winning strategy is persisted immediately (`strategy_source="learned"`)
        so the very next run of this source starts from it. On failure only
        the ladder pointer advances, and two consecutive failed audits (this
        run plus a future one, or two attempts within the same run's ladder)
        flags the profile and forces a fresh probe next time — see
        record_recall_outcome / needs_reprobe. Never loops: each page gets at
        most one retry attempt per call to this method.
        """
        from config import settings
        from processing.site_profile_store import (
            get_profile, next_retry_step, apply_retry_result, record_recall_outcome,
        )
        from ingestion.worker_queue import PageItem, chunker_task, qwen_worker_task, storage_worker_task

        shortfalls = metrics.shortfall_pages(
            ratio=settings.recall_shortfall_ratio, min_gap=settings.recall_shortfall_min_gap,
        )
        if not shortfalls:
            return

        # Snapshot BEFORE any retry runs — metrics.per_page holds live
        # PageOutcome references, not copies, and a retry mutates `extracted`
        # on the SAME object (record_extraction just grows the set), so the
        # pre-retry count must be captured now or it's lost.
        candidates = [(o, o.expected, len(o.extracted)) for o in shortfalls[:settings.recall_retry_max_pages]]
        calls_budget = settings.recall_retry_max_calls

        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=self._http_timeout, follow_redirects=True, max_redirects=5,
        ) as client:
            for outcome, expected, old_count in candidates:
                metrics.inc("recall_shortfalls")
                if calls_budget <= 0:
                    logger.info("[Scraper] Recall retry call budget exhausted — skipping remaining shortfalls")
                    break

                profile = get_profile(outcome.url)
                if profile is None:
                    continue  # defensive: expected>0 should always imply a profile exists

                step = next_retry_step(profile, outcome, metrics)
                if step is None:
                    logger.info(f"[Scraper] Retry ladder exhausted for {outcome.url} — recording failure")
                    record_recall_outcome(str(profile.id), expected=expected, extracted=old_count, recovered=False)
                    continue

                ladder_idx, new_strategy = step
                metrics.inc("retries_attempted")
                logger.info(
                    f"[Scraper] Recall retry step {ladder_idx} for {outcome.url} "
                    f"({old_count}/{expected} extracted so far)"
                )

                html = await self._fetch_page(
                    client, outcome.url,
                    force_render=new_strategy.needs_render, paginate=new_strategy.paginate,
                    metrics=metrics,
                )
                if not html:
                    apply_retry_result(str(profile.id), ladder_idx, new_strategy, recovered=False)
                    record_recall_outcome(str(profile.id), expected=expected, extracted=old_count, recovered=False)
                    continue

                content = extract_content(html, outcome.url, new_strategy)

                # Pre-flight cost check for the two structural chunk kinds,
                # where the exact call count is knowable before running
                # anything (one call per card, or ceil(names/batch_size) for
                # a name batch) — found live 3 Aug: halving names_per_chunk
                # on zollhof's 117-item grid alone needed 39 calls, already
                # over the whole batch's 30-call ceiling on its own. The
                # per-candidate budget check above only stops the NEXT
                # candidate, not one already in flight, so this page would
                # have silently overshot without a pre-flight estimate.
                if new_strategy.chunking == "name_batch" and content.entity_names:
                    n = new_strategy.names_per_chunk or 6
                    est_calls = -(-len(content.entity_names) // max(1, n))  # ceil
                elif new_strategy.chunking == "per_card" and content.entity_blocks:
                    est_calls = len(content.entity_blocks)
                else:
                    est_calls = None  # prose/full_text: bounded by content length in practice, not pre-estimated

                if est_calls is not None and est_calls > calls_budget:
                    logger.info(
                        f"[Scraper] Recall retry for {outcome.url} would need ~{est_calls} calls, "
                        f"only {calls_budget} left in budget — skipping rather than overshoot"
                    )
                    record_recall_outcome(str(profile.id), expected=expected, extracted=old_count, recovered=False)
                    continue

                page_item = PageItem(
                    url=outcome.url, text=content.text, source_type=source_type, source_url=source_url,
                    strategy=new_strategy, entity_names=content.entity_names, entity_blocks=content.entity_blocks,
                    expected_entity_count=expected,
                )

                # A tiny dedicated single-page mini-pipeline through the SAME
                # stage functions the real crawl used — guarantees identical
                # filter-bypass rules, StorageItem construction, and real
                # upsert_startup writes, with no duplicated logic.
                pq, cq, sq = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
                await pq.put(page_item)
                await pq.put(None)
                calls_before = metrics.qwen_calls
                await asyncio.gather(
                    chunker_task(pq, cq, metrics),
                    qwen_worker_task(cq, sq, metrics, 0, validation_session=validation_session),
                    storage_worker_task(sq, metrics, 1, validation_session=validation_session),
                )
                calls_budget -= (metrics.qwen_calls - calls_before)

                new_count = len(outcome.extracted)  # same PageOutcome, now grown by the retry
                new_ratio = (new_count / expected) if expected else 1.0
                recovered = (
                    new_ratio >= settings.recall_shortfall_ratio
                    or (expected - new_count) < settings.recall_shortfall_min_gap
                )
                if recovered:
                    metrics.inc("retries_recovered")
                    logger.info(f"[Scraper] Recall retry recovered {outcome.url}: {new_count}/{expected}")
                else:
                    logger.info(f"[Scraper] Recall retry did not recover {outcome.url}: {new_count}/{expected}")

                apply_retry_result(str(profile.id), ladder_idx, new_strategy, recovered=recovered)
                record_recall_outcome(str(profile.id), expected=expected, extracted=new_count, recovered=recovered)

    # ── BFS Crawler Task ──────────────────────────────────────────────────────

    async def _crawler_task(
        self,
        start_url: str,
        source_type: str,
        max_depth: int,
        max_pages: int,
        page_queue: asyncio.Queue,
        metrics: "PipelineMetrics",
        *,
        force_render: bool = False,
    ) -> None:
        """
        Asynchronous BFS crawler — Stage 1 of the worker pipeline.

        Fetches pages and puts PageItems into page_queue as each page is
        retrieved, so the chunker and Qwen workers can start processing
        immediately without waiting for the entire crawl to complete.

        Puts the None sentinel into page_queue when the BFS is exhausted.
        Domain isolation, SKIP_PATTERNS filtering, and Playwright fallback
        are all preserved from the original _deep_crawl implementation.
        """
        from ingestion.worker_queue import PageItem
        from config import settings
        from ingestion.strategy import PageStrategy, ENTITY_SHAPES

        adaptive = settings.adaptive_pipeline_enabled

        allowed_domain = _base_domain(start_url)
        visited: set = set()
        queued: set = {start_url}   # everything ever enqueued (dedupe the frontier)
        # Two-tier priority frontier: high-value startup/portfolio pages
        # (_url_priority == 0) are drained BEFORE generic section/nav pages, so
        # the page budget reaches actual startup content instead of being spent
        # on "about / events / news" first. FIFO within each tier keeps the
        # crawl breadth-first and stable.
        frontier_high: deque = deque()
        frontier_low: deque = deque([(start_url, 0)])

        def _next():
            if frontier_high:
                return frontier_high.popleft()
            if frontier_low:
                return frontier_low.popleft()
            return None

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=self._http_timeout,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            while (frontier_high or frontier_low) and len(visited) < max_pages:
                current_url, depth = _next()

                if current_url in visited:
                    continue
                visited.add(current_url)

                strategy = PageStrategy.DEFAULT
                known_profile = None
                if adaptive:
                    from processing.site_profile_store import get_profile, strategy_from_profile
                    known_profile = get_profile(current_url)
                    if known_profile is not None:
                        metrics.inc("profile_hits")
                        strategy = strategy_from_profile(known_profile)
                    else:
                        metrics.inc("profile_misses")

                # A known profile can request rendering/pagination for THIS
                # specific page even when the source-level force_render
                # flag is off (Phase R-4 — needs_render/paginate are now
                # independent, not welded to the source-level flag). A page
                # with no profile yet uses today's exact fetch heuristic so
                # its first crawl is never worse than before this phase.
                page_force_render = force_render or (known_profile is not None and strategy.needs_render)
                page_paginate = force_render or (known_profile is not None and strategy.paginate)
                _fetch_t0 = time.time()
                html = await self._fetch_page(
                    client, current_url,
                    force_render=page_force_render, paginate=page_paginate,
                    metrics=metrics if adaptive else None,
                )
                metrics.inc("fetch_time_s", time.time() - _fetch_t0)
                if not html:
                    metrics.inc("pages_skipped")
                    continue

                if adaptive and known_profile is None:
                    # First time this pattern has been seen: derive a
                    # strategy from the HTML we already fetched (no extra
                    # network I/O, no LLM, no GPU mutex) and persist it so
                    # every later run — and the dashboard — has it. The
                    # richer LLM-adjudicated verdict still comes from the
                    # existing probe_and_store() path (its own independent
                    # probe), triggered separately by the dashboard's
                    # "Profile all sources" / "Re-inspect" actions.
                    try:
                        from processing.site_profile_store import store_deterministic, strategy_from_profile
                        new_profile = store_deterministic(current_url, html)
                        strategy = strategy_from_profile(new_profile)
                        metrics.inc("profile_probes")
                    except Exception as exc:
                        logger.debug(f"[Scraper] deterministic profile derivation failed for {current_url}: {exc}")
                elif adaptive and known_profile is not None and strategy.page_shape in ENTITY_SHAPES:
                    # expected_entity_count must be recomputed fresh from
                    # THIS page's actual current content on every run — never
                    # trusted from the cached profile — so a site redesign is
                    # noticed even though the STRATEGY (how to process the
                    # page) is still reused from the cache. This is the R-0
                    # safety property ("a cached count cannot notice a
                    # redesign"); the R-5 recall audit depends on it being
                    # real, not stale, or a shortfall would only ever measure
                    # "did we match the last probe" rather than "did we match
                    # what's actually here right now."
                    try:
                        from ingestion.site_inspector import probe_html
                        from config.tuning_loader import get_inspector_config
                        fresh_sig = probe_html(html, current_url, get_inspector_config())
                        strategy = strategy.with_(expected_entity_count=fresh_sig.candidate_entity_count)
                    except Exception as exc:
                        logger.debug(f"[Scraper] fresh entity-count recompute failed for {current_url}: {exc}")

                content = extract_content(html, current_url, strategy) if adaptive else PageContent(text=_extract_text(html))

                if content.text or content.entity_names or content.entity_blocks:
                    if adaptive and strategy.expects_entities:
                        metrics.record_expectation(
                            current_url, strategy.expected_entity_count, shape=strategy.page_shape,
                        )
                    await page_queue.put(PageItem(
                        url=current_url,
                        text=content.text,
                        source_type=source_type,
                        source_url=start_url,
                        strategy=strategy if adaptive else None,
                        entity_names=content.entity_names,
                        entity_blocks=content.entity_blocks,
                        expected_entity_count=strategy.expected_entity_count if adaptive else 0,
                    ))
                    metrics.inc("pages_crawled")
                    logger.debug(
                        f"[Scraper] [{depth}] Crawled {current_url} "
                        f"({len(content.text)} chars, {len(content.entity_names)} entities)"
                    )
                else:
                    metrics.inc("pages_skipped")

                # Only enqueue children if we haven't reached max depth
                if depth < max_depth:
                    for link in _extract_links(html, current_url):
                        if link in queued or _base_domain(link) != allowed_domain:
                            continue
                        if _is_irrelevant_url(link):
                            logger.debug(f"[Scraper] Skipping irrelevant URL: {link}")
                            continue
                        queued.add(link)
                        item = (link, depth + 1)
                        if _url_priority(link) == 0:
                            frontier_high.append(item)
                        else:
                            frontier_low.append(item)

        logger.info(
            f"[Scraper] Crawl complete — {metrics.pages_crawled} pages "
            f"scraped from {allowed_domain}"
        )
        await page_queue.put(None)  # sentinel: signals chunker that crawl is done

    # ── Fetch Strategies ──────────────────────────────────────────────────────

    async def _fetch_page(self, client: httpx.AsyncClient, url: str, *,
                          force_render: bool = False, paginate: bool = False,
                          metrics: "PipelineMetrics" = None) -> str:
        """
        Fetch a single page.

        force_render=True (source render_mode "always", or Phase R-4's
        per-page SiteProfile.needs_render): skip the static fetch entirely
        and render in a headless browser — for JS directory sites (React/
        Vue/Next) whose content is invisible to a plain fetch.

        Otherwise: fast static httpx fetch, falling back to Playwright only
        when the static fetch fails outright OR "succeeds" with a 200 but
        yields an empty client-side-rendering shell (< _MIN_STATIC_TEXT_LEN).

        paginate is independent of force_render (Phase R-4 — previously
        welded together, so a page auto-escalated to Playwright for being a
        thin shell was never paginated even if it structurally needed it):
        whichever path ends up rendering, paginate controls whether that
        render also exhausts a "load more"/infinite-scroll list.
        """
        if force_render:
            return await self._fetch_playwright(url, paginate=paginate, metrics=metrics)

        try:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "text/html" in content_type:
                html = resp.text
                if len(_extract_text(html)) >= _MIN_STATIC_TEXT_LEN:
                    if metrics is not None:
                        metrics.inc("pages_static")
                    return html
                logger.debug(
                    f"[Scraper] {url} — static fetch too thin "
                    f"({len(_extract_text(html))} chars), trying Playwright"
                )
            else:
                logger.debug(
                    f"[Scraper] Skipped {url} — "
                    f"status={resp.status_code} content-type={content_type}"
                )
        except Exception as exc:
            logger.debug(f"[Scraper] httpx failed for {url}: {exc}")

        # Playwright fallback for JS-gated / Cloudflare-protected / SPA sites
        return await self._fetch_playwright(url, paginate=paginate, metrics=metrics)

    async def _fetch_playwright(self, url: str, *, paginate: bool = False,
                                metrics: "PipelineMetrics" = None) -> str:
        """
        JavaScript-aware fetch using Playwright (headless Chromium).

        Waits for the network to settle (so a client-side-loaded startup grid
        actually appears) plus a short render beat, then snapshots the DOM.
        The networkidle wait is bounded and best-effort: some sites never go
        idle (perpetual analytics/chat/websocket traffic), so a timeout there
        is expected and we proceed to snapshot whatever has rendered rather
        than failing — measured on munich-startup.de, this lifts a directory
        page from a 1.5 KB shell to ~11 KB of real content.

        paginate=True: after the first render, exhaust the list by repeatedly
        clicking a "load more" button (or scrolling for infinite-scroll lists)
        until it stops growing or the configured cap is hit — measured on
        munich-startup.de, this lifts a directory from ~11 KB (first page,
        ~12 startups) to ~350 KB (the full list). Bounded and self-stopping,
        so a non-paginated page costs at most one extra scroll.
        """
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_extra_http_headers({"User-Agent": _HEADERS["User-Agent"]})
                # domcontentloaded first (fast, always resolves), THEN try to
                # let async data-loading settle — don't hang forever on it.
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass  # never went idle — snapshot what rendered anyway
                await page.wait_for_timeout(1_500)  # brief beat for final paint
                await self._dismiss_cookie_banner(page)

                if paginate:
                    await self._exhaust_pagination(page, url, metrics=metrics)

                html = await page.content()
                await browser.close()
            if metrics is not None:
                metrics.inc("pages_rendered")
            return html
        except Exception as exc:
            logger.error(f"[Scraper] Playwright failed {url}: {exc}")
        return ""

    async def _dismiss_cookie_banner(self, page) -> None:
        """
        Best-effort: click a cookie-consent accept button if one is visible.
        Never raises — a banner that isn't found, or doesn't dismiss cleanly,
        just leaves the page as it was; this only ever helps, never blocks.
        """
        for pat in _COOKIE_ACCEPT_PATTERNS:
            loc = page.locator(f'button:has-text("{pat}")').first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2_000)
                    await page.wait_for_timeout(800)
                    return
            except Exception:
                continue

    async def _exhaust_pagination(self, page, url: str, *,
                                  metrics: "PipelineMetrics" = None) -> None:
        """
        Repeatedly reveal more of a paginated list: click a "load more" button
        if present, else scroll to the bottom (infinite scroll). Stop when the
        page height stops growing for two consecutive rounds, or the configured
        click cap is reached. Fully bounded — never loops forever, and does
        almost nothing on a page that isn't a growing list.
        """
        from config import settings

        cap = settings.crawl_max_load_more
        stagnant = 0
        clicks = 0
        for _ in range(cap):
            try:
                before = await page.evaluate("document.body.scrollHeight")
            except Exception:
                break

            clicked = False
            for pat in _LOAD_MORE_PATTERNS:
                loc = page.locator(f'button:has-text("{pat}"), a:has-text("{pat}")').first
                try:
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=2_500)
                        clicked = True
                        clicks += 1
                        break
                except Exception:
                    continue  # button vanished/detached mid-loop — try next pattern

            if not clicked:
                # No load-more control — try infinite-scroll instead.
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    break

            await page.wait_for_timeout(1_200)  # let new items render

            try:
                after = await page.evaluate("document.body.scrollHeight")
            except Exception:
                break

            if after <= before:
                stagnant += 1
                if stagnant >= 2:
                    break  # nothing new twice running — list is exhausted
            else:
                stagnant = 0
        else:
            # Loop ran out of its click budget WITHOUT any break above — the
            # page was still growing when the cap stopped us, not naturally
            # exhausted. Phase R-5's retry-ladder step 4 signal: a higher
            # max_load_more is worth trying, as opposed to a page that
            # simply had nothing more to reveal.
            if metrics is not None and clicks > 0:
                metrics.pagination_hit_cap.add(url)

        if clicks:
            logger.info(f"[Scraper] Paginated {url} — {clicks} 'load more' step(s)")
        if metrics is not None and clicks:
            metrics.inc("pagination_clicks", clicks)


web_scraper = WebScraper()
