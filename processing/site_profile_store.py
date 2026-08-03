"""
SiteProfile persistence + lookup (Phase R-2, 31 Jul — self-adapting web
extraction).

The SiteProfile table is the pipeline's LEARNED per-source state — what
Phase R-1's structural inspector (and, from R-3, the cached LLM strategist)
concluded about a page's shape — kept separate from human-authored
config/sources.yaml (owner decision, 31 Jul) so an automatic probe can never
clobber a hand edit, and a site redesign is noticeable without a code change.

Keying: startups store `source_url` strings with no FK to a registry
source_id, so a profile must be findable from a bare URL alone. Every profile
is keyed on (domain, url_pattern) — domain is the lowercased, www-stripped
netloc; url_pattern is "" for a domain's default page shape, or a normalized
path template ("/startups/*") for a listing shape distinct from the rest of
the domain. get_profile() returns the most specific match: an exact path
pattern first, then the domain default, then None.

Nothing in the live extraction path calls ensure_profile()/strategy_from_
profile() yet — that wiring is Phase R-4. This module is exercised today only
by the dashboard's profiling routes (api/routes/sources.py) and
scripts/inspect_site.py.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from config import settings
from database.connection import SessionLocal
from database.models import SiteProfile

logger = logging.getLogger(__name__)

# A trailing path segment that looks like a detail-page slug: multi-word
# hyphenated ("acme-gmbh", "story-42-launch") or purely numeric ("123").
# Single-URL heuristic — no cross-page knowledge needed, which is why
# "/startup-incubation/portfolio" (no hyphen in "portfolio") correctly stays
# literal: that IS the listing page, not one instance of a detail page.
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


def normalize_domain(url: str) -> str:
    """Lowercased netloc, www. stripped."""
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_path_pattern(url: str) -> str:
    """
    "" for a domain-default page (bare domain, or "/"). Otherwise a path
    template with the trailing segment collapsed to "*" when it looks like a
    detail-page slug. "/startups/acme-gmbh" -> "/startups/*";
    "/news-details/story-42" -> "/news-details/*";
    "/startup-incubation/portfolio" -> unchanged (the listing page itself).
    """
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    last = segments[-1].lower()
    if last.isdigit() or _SLUG_RE.fullmatch(last):
        segments[-1] = "*"
    return "/" + "/".join(segments)


def _resolve_source_id(url: str) -> Optional[str]:
    """Best-effort: does this URL's domain match a registered source?"""
    try:
        from config.source_loader import get_web_sources
        domain = normalize_domain(url)
        for s in get_web_sources():
            if normalize_domain(s.primary_url) == domain:
                return s.source_id
    except Exception:
        pass
    return None


def get_profile(url: str, db=None) -> Optional[SiteProfile]:
    """Most specific match: exact path pattern -> domain default -> None."""
    owns = db is None
    db = db or SessionLocal()
    try:
        domain = normalize_domain(url)
        pattern = normalize_path_pattern(url)
        if pattern:
            hit = db.query(SiteProfile).filter_by(domain=domain, url_pattern=pattern).first()
            if hit:
                return hit
        return db.query(SiteProfile).filter_by(domain=domain, url_pattern="").first()
    finally:
        if owns:
            db.close()


def needs_reprobe(profile: Optional[SiteProfile]) -> bool:
    """
    Whether a profile is due for a fresh probe. status=="pinned" is the one
    unconditional exception — the manual override that replaces per-site
    hand-tuning never expires or self-corrects; a human put it there on
    purpose (mirrors the old sources.yaml render_mode: "always" escape hatch).
    """
    if profile is None:
        return True
    if profile.status == "pinned":
        return False
    if profile.status == "stale":
        return True
    from ingestion.site_inspector import INSPECTOR_VERSION
    if (profile.probe_version or 0) < INSPECTOR_VERSION:
        return True
    if (profile.consecutive_shortfalls or 0) >= 2:
        return True
    ttl_days = getattr(settings, "site_profile_ttl_days", 30)
    if profile.probed_at and datetime.utcnow() - profile.probed_at > timedelta(days=ttl_days):
        return True
    return False


async def probe_and_store(url: str, *, force: bool = False, client=None) -> SiteProfile:
    """
    Probe a URL and persist the result, creating or updating the matching
    profile. Deterministic-only in R-2 — R-3 layers the cached LLM strategist
    on top without changing this function's contract or its callers.

    A pinned profile is refreshed only for last_used_at; its strategy is
    never touched by an automatic probe, force or not — force overrides the
    TTL/version/shortfall re-probe policy, not the pin itself. To actually
    change a pinned profile's strategy, unpin it first.
    """
    from ingestion.site_inspector import (
        INSPECTOR_VERSION, probe_url as _probe_url, derive_strategy_deterministic,
    )
    from config.tuning_loader import get_inspector_config

    db = SessionLocal()
    try:
        domain = normalize_domain(url)
        pattern = normalize_path_pattern(url)
        row = db.query(SiteProfile).filter_by(domain=domain, url_pattern=pattern).first()

        if row is not None and row.status == "pinned":
            row.last_used_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return row

        if row is not None and not force and not needs_reprobe(row):
            row.last_used_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return row

        cfg = get_inspector_config()
        # A profile already known to need rendering skips straight to it —
        # no point re-attempting a static fetch we already know is thin.
        already_renders = bool(row and row.needs_render)
        sig = await _probe_url(url, client=client, force_render=already_renders, cfg=cfg)
        strat = derive_strategy_deterministic(sig, cfg)

        # Phase R-3: the LLM only ADJUDICATES this — it never runs before the
        # deterministic pass above and its failure never blocks a profile
        # from being written. expected_entity_count is never touched by it;
        # that stays purely structural, set from `strat` (or a speculative
        # profile's own later real probe), never the model's output.
        extra_profiles = []
        if getattr(settings, "site_strategy_llm_enabled", True):
            strat, extra_profiles = _adjudicate_with_llm(url, sig, strat, pattern)

        if row is None:
            row = SiteProfile(domain=domain, url_pattern=pattern)
            db.add(row)

        _apply_strategy(row, strat, source_id=_resolve_source_id(url), signals=sig.to_dict())
        db.commit()

        # Store speculative (unprobed) profiles BEFORE the final refresh, not
        # after: each one does its own db.commit(), and SQLAlchemy's default
        # expire_on_commit=True expires every object already loaded into this
        # session on EVERY commit — including `row`, previously refreshed.
        # Refreshing here first would leave `row` expired again by the time
        # this function returns, raising DetachedInstanceError on the
        # caller's very first attribute access after db.close(). Found live
        # 3 Aug testing against schwaben.digital, whose detail_link_pattern
        # (/events/*) is exactly the case that produces a speculative entry.
        for ep in extra_profiles:
            _store_speculative_profile(db, domain, ep)
        db.refresh(row)

        return row
    finally:
        db.close()


def store_deterministic(url: str, html: str) -> SiteProfile:
    """
    Persist a profile derived ONLY from the deterministic inspector, reusing
    HTML the crawler already fetched — no extra network I/O, no LLM, no GPU
    mutex (Phase R-4). This is what lets a brand-new source get a usable
    strategy on its very first crawl (the R-7 "cold add, zero tuning"
    requirement) without the live crawl loop ever touching Ollama.

    The richer LLM-adjudicated verdict, and a proper independent (static +
    rendered) probe for render_gain, still come from probe_and_store() —
    the dashboard's "Profile all sources" / "Re-inspect" actions — which
    this function deliberately does not duplicate or replace.

    Never clobbers an existing row: if a profile for this exact
    (domain, url_pattern) already appeared (a probe, or another page in the
    same crawl hitting the same pattern first), that row wins.
    """
    from ingestion.site_inspector import probe_html, derive_strategy_deterministic
    from config.tuning_loader import get_inspector_config

    db = SessionLocal()
    try:
        domain = normalize_domain(url)
        pattern = normalize_path_pattern(url)
        row = db.query(SiteProfile).filter_by(domain=domain, url_pattern=pattern).first()
        if row is not None:
            return row

        cfg = get_inspector_config()
        sig = probe_html(html, url, cfg)
        strat = derive_strategy_deterministic(sig, cfg)

        row = SiteProfile(domain=domain, url_pattern=pattern)
        db.add(row)
        _apply_strategy(row, strat, source_id=_resolve_source_id(url), signals=sig.to_dict())
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _apply_strategy(row: SiteProfile, strat, *, source_id, signals) -> None:
    """Write a PageStrategy onto a SiteProfile row. Shared by the real probe
    path above and the speculative (unprobed) profile path below."""
    from ingestion.site_inspector import INSPECTOR_VERSION

    row.source_id = source_id
    row.page_shape = strat.page_shape
    row.text_extraction = strat.text_extraction
    row.chunking_mode = strat.chunking
    row.needs_render = strat.needs_render
    row.paginate = strat.paginate
    row.follow_detail_links = strat.follow_detail_links
    row.detail_link_pattern = strat.detail_link_pattern
    row.bypass_candidate_filter = strat.bypass_candidate_filter
    row.load_more_selector = strat.load_more_selector
    row.expected_entity_count = strat.expected_entity_count
    row.strategy_source = strat.source
    row.confidence = strat.confidence
    row.reason = strat.reason
    row.signals = signals
    row.probe_version = INSPECTOR_VERSION
    row.probed_at = datetime.utcnow()
    row.last_used_at = datetime.utcnow()
    row.status = "active"
    row.consecutive_shortfalls = 0
    row.flag_reason = None


def _adjudicate_with_llm(url: str, sig, deterministic_strat, own_pattern: str):
    """
    Ask the cached strategist to confirm/correct the deterministic verdict.
    Returns (strategy_for_this_page, [extra PageStrategy for OTHER patterns
    the model inferred, e.g. a detail_link_pattern]).

    Never raises — a failed/unreachable/malformed call logs a warning and
    returns the deterministic strategy unchanged, exactly as if the kill
    switch were off. This function is the ONLY place the override rule
    lives: an LLM verdict disagreeing with a HIGH-confidence deterministic
    signal loses on text_extraction/chunking specifically (those are what
    actually drive extraction correctness); page_shape and the boolean flags
    still take the LLM's adjudication, since that's the label/behaviour it
    was asked to correct.
    """
    from reasoning.qwen_client import qwen_client
    from ingestion.strategy import PageStrategy

    try:
        profiles = qwen_client.decide_site_strategy(
            url, sig.to_dict(), deterministic_strat.to_dict(), own_pattern,
        )
    except Exception as exc:
        logger.warning(f"[SiteStrategy] LLM adjudication failed for {url}: {exc} — keeping deterministic")
        return deterministic_strat, []

    # Position, not string-matching, decides which entry is "this page": the
    # prompt instructs the model to always put it first, and a 7B model's
    # echo of a literal pattern string (especially the "(domain default)"
    # placeholder used for an empty pattern) isn't reliable enough to match
    # against — confirmed live: schwaben.digital's response echoed
    # "(domain default)" verbatim as url_pattern for what was unambiguously
    # meant to be the "own" entry. Matching on it would have silently
    # mis-sorted own vs. speculative for any multi-profile response.
    own = profiles[0]
    others = profiles[1:]

    llm_strat = PageStrategy(
        page_shape=own.get("page_shape", deterministic_strat.page_shape),
        text_extraction=own.get("text_extraction", deterministic_strat.text_extraction),
        chunking=own.get("chunking", deterministic_strat.chunking),
        needs_render=bool(own.get("needs_render", deterministic_strat.needs_render)),
        paginate=bool(own.get("paginate", deterministic_strat.paginate)),
        follow_detail_links=bool(own.get("follow_detail_links", deterministic_strat.follow_detail_links)),
        detail_link_pattern=deterministic_strat.detail_link_pattern,  # structural fact, not the LLM's to set
        bypass_candidate_filter=bool(own.get("bypass_candidate_filter", deterministic_strat.bypass_candidate_filter)),
        expected_entity_count=deterministic_strat.expected_entity_count,  # NEVER from the LLM
        confidence=own.get("confidence", "low"),
        reason=own.get("reason", ""),
        source="llm",
    )

    disagrees = (
        llm_strat.text_extraction != deterministic_strat.text_extraction
        or llm_strat.chunking != deterministic_strat.chunking
    )
    if disagrees and deterministic_strat.confidence == "high":
        logger.info(
            f"[SiteStrategy] LLM disagreed with a high-confidence deterministic "
            f"verdict for {url} ({deterministic_strat.text_extraction}/{deterministic_strat.chunking} "
            f"vs {llm_strat.text_extraction}/{llm_strat.chunking}) — deterministic wins on "
            f"text_extraction/chunking; keeping the LLM's page_shape label and reasoning."
        )
        final = llm_strat.with_(
            text_extraction=deterministic_strat.text_extraction,
            chunking=deterministic_strat.chunking,
            source="llm_overridden",
        )
    else:
        final = llm_strat

    extra_strats = []
    for p in others:
        extra_strats.append((
            p.get("url_pattern", ""),
            PageStrategy(
                page_shape=p.get("page_shape", "unknown"),
                text_extraction=p.get("text_extraction", "full_text"),
                chunking=p.get("chunking", "sliding_window"),
                needs_render=bool(p.get("needs_render", False)),
                paginate=bool(p.get("paginate", False)),
                follow_detail_links=bool(p.get("follow_detail_links", False)),
                bypass_candidate_filter=bool(p.get("bypass_candidate_filter", False)),
                expected_entity_count=0,  # never probed — no structural basis for a count
                confidence=p.get("confidence", "low"),
                reason=p.get("reason", ""),
                source="llm",
            ),
        ))
    return final, extra_strats


def _store_speculative_profile(db, domain: str, entry) -> None:
    """
    Persist a profile for a pattern the strategist inferred but that has
    never actually been structurally probed (e.g. "detail pages under
    /startups/* probably look like this"). Left as-is if a real profile
    already exists for this pattern — a genuine probe always outranks a
    speculative guess, and this must never clobber one.
    """
    pattern, strat = entry
    if not pattern:
        return
    existing = db.query(SiteProfile).filter_by(domain=domain, url_pattern=pattern).first()
    if existing is not None:
        return
    row = SiteProfile(domain=domain, url_pattern=pattern)
    db.add(row)
    _apply_strategy(row, strat, source_id=None, signals=None)
    row.probed_at = None  # never actually probed — distinguishes it from a real profile
    db.commit()


def strategy_from_profile(profile: Optional[SiteProfile]):
    """
    Turn a persisted profile into the runtime PageStrategy the pipeline
    consumes (Phase R-4). None -> PageStrategy.DEFAULT, today's exact
    behaviour — an unprofiled source is never worse off than before R-1.
    """
    from ingestion.strategy import PageStrategy

    if profile is None:
        return PageStrategy.DEFAULT
    return PageStrategy(
        page_shape=profile.page_shape or "unknown",
        text_extraction=profile.text_extraction or "full_text",
        chunking=profile.chunking_mode or "sliding_window",
        needs_render=bool(profile.needs_render),
        paginate=bool(profile.paginate),
        follow_detail_links=bool(profile.follow_detail_links),
        detail_link_pattern=profile.detail_link_pattern,
        bypass_candidate_filter=bool(profile.bypass_candidate_filter),
        names_per_chunk=profile.names_per_chunk,
        load_more_selector=profile.load_more_selector,
        max_pages=profile.max_pages,
        max_depth=profile.max_depth,
        max_load_more=profile.max_load_more,
        expected_entity_count=profile.expected_entity_count or 0,
        profile_id=str(profile.id),
        confidence=profile.confidence or "low",
        reason=profile.reason or "",
        source=profile.strategy_source or "learned",
    )


def record_recall_outcome(profile_id: str, *, expected: int, extracted: int, recovered: bool) -> None:
    """
    Phase R-5 hook: update a profile's feedback columns after a page's
    expected-vs-actual is known (and, if it was short, after a retry was
    attempted). Two consecutive shortfalls -> flagged + marked stale, so the
    next run gets a fresh probe instead of repeating the same bad strategy.
    """
    db = SessionLocal()
    try:
        row = db.query(SiteProfile).filter_by(id=profile_id).first()
        if row is None:
            return
        row.last_expected = expected
        row.last_extracted = extracted
        row.recall_ratio = (extracted / expected) if expected else 1.0
        if recovered or row.recall_ratio >= 1.0:
            row.consecutive_shortfalls = 0
        else:
            row.consecutive_shortfalls = (row.consecutive_shortfalls or 0) + 1
            if row.consecutive_shortfalls >= 2 and row.status != "pinned":
                row.status = "flagged"
                row.flag_reason = (
                    f"recall shortfall after retry: {extracted}/{expected} "
                    f"({row.consecutive_shortfalls} consecutive)"
                )
        db.commit()
    finally:
        db.close()


def next_retry_step(profile: SiteProfile, page_outcome, metrics) -> Optional[tuple]:
    """
    Phase R-5 retry ladder — deterministic, most-certain evidence first.
    Returns (new_ladder_position, PageStrategy) for the first untried
    applicable step from profile.retry_ladder_position onward, or None when
    every remaining step has been exhausted (the caller should treat that as
    a plain failure, not attempt anything further).

    Checking qwen_failures FIRST is essential: a timeout shortfall and a
    fetch shortfall look identical in the raw expected-vs-extracted numbers
    but need opposite fixes — halving the batch size fixes a timeout;
    re-fetching with rendering fixes a genuinely-missing page. Trying
    rendering first on a timeout-caused shortfall would burn a retry
    attempt on a page that was already being fetched correctly.
    """
    from config import settings
    from config.tuning_loader import get_chunking_config

    strat = strategy_from_profile(profile)
    start = (profile.retry_ladder_position or 0) + 1

    def _default_names_per_chunk() -> int:
        try:
            return int(get_chunking_config().get("names_per_chunk", 6))
        except Exception:
            return 6

    steps = (
        (1, page_outcome.qwen_failures > 0,
         lambda s: s.with_(names_per_chunk=max(1, (s.names_per_chunk or _default_names_per_chunk()) // 2))),
        (2, not strat.needs_render,
         lambda s: s.with_(needs_render=True, paginate=True)),
        (3, strat.needs_render and not strat.paginate,
         lambda s: s.with_(paginate=True)),
        (4, page_outcome.url in getattr(metrics, "pagination_hit_cap", set()),
         lambda s: s.with_(max_load_more=(s.max_load_more or settings.crawl_max_load_more) * 2)),
        (5, strat.text_extraction == "card_structured",
         lambda s: s.with_(text_extraction="alt_harvest", chunking="name_batch")),
        (6, strat.text_extraction == "alt_harvest",
         lambda s: s.with_(text_extraction="card_structured", chunking="per_card")),
        (7, strat.text_extraction == "main_prose",
         lambda s: s.with_(text_extraction="full_text", chunking="sliding_window")),
    )
    for idx, condition, transform in steps:
        if idx < start:
            continue
        if condition:
            return idx, transform(strat)
    return None


def apply_retry_result(profile_id: str, ladder_idx: int, new_strategy, *, recovered: bool) -> None:
    """
    Persist the outcome of one R-5 retry attempt. On success, the winning
    strategy fields become the profile's live strategy (strategy_source
    "learned") so the next real run starts there. On failure, only the
    ladder pointer advances — the live strategy is untouched, so a
    subsequent retry (this run or a future one, after the profile is marked
    stale by record_recall_outcome's consecutive-shortfall handling) tries
    the NEXT applicable step rather than repeating one already proven not to
    help. Never touches page_shape/expected_entity_count — those stay
    exactly what the last real structural probe determined.
    """
    db = SessionLocal()
    try:
        row = db.query(SiteProfile).filter_by(id=profile_id).first()
        if row is None:
            return
        row.retry_ladder_position = ladder_idx
        if recovered:
            row.text_extraction = new_strategy.text_extraction
            row.chunking_mode = new_strategy.chunking
            row.needs_render = new_strategy.needs_render
            row.paginate = new_strategy.paginate
            row.names_per_chunk = new_strategy.names_per_chunk
            row.max_load_more = new_strategy.max_load_more
            row.strategy_source = "learned"
        db.commit()
    finally:
        db.close()


def set_pinned(profile_id: str, pinned: bool) -> Optional[SiteProfile]:
    """Dashboard pin/unpin — the human escape hatch."""
    db = SessionLocal()
    try:
        row = db.query(SiteProfile).filter_by(id=profile_id).first()
        if row is None:
            return None
        row.status = "pinned" if pinned else "active"
        if not pinned:
            row.consecutive_shortfalls = 0
            row.flag_reason = None
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def mark_stale(profile_id: str = None, *, url: str = None) -> Optional[SiteProfile]:
    """Manual "Re-inspect" trigger — flips a profile to stale so the next
    probe_and_store() call actually re-probes instead of reusing the cache."""
    db = SessionLocal()
    try:
        row = None
        if profile_id:
            row = db.query(SiteProfile).filter_by(id=profile_id).first()
        elif url:
            row = get_profile(url, db)
        if row is None:
            return None
        if row.status == "pinned":
            return row  # pin wins; re-inspect is a no-op until unpinned
        row.status = "stale"
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def list_profiles(db=None) -> list:
    owns = db is None
    db = db or SessionLocal()
    try:
        return db.query(SiteProfile).order_by(SiteProfile.domain, SiteProfile.url_pattern).all()
    finally:
        if owns:
            db.close()
