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

        if row is None:
            row = SiteProfile(domain=domain, url_pattern=pattern)
            db.add(row)

        row.source_id = _resolve_source_id(url)
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
        row.signals = sig.to_dict()
        row.probe_version = INSPECTOR_VERSION
        row.probed_at = datetime.utcnow()
        row.last_used_at = datetime.utcnow()
        row.status = "active"
        row.consecutive_shortfalls = 0
        row.flag_reason = None
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


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
