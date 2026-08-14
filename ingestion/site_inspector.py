"""
Structural page inspection (Phase R-1) — how the pipeline learns a site's shape
instead of being hand-tuned to it.

Why this exists
---------------
Before this module, every content-derived decision in the pipeline was two
integers: `_MIN_STATIC_TEXT_LEN = 200` and `_MIN_LOGO_GRID_ENTRIES = 12`.
Tuning fitted to one page shape (zollhof.de's clean 120-company logo grid,
30 Jul) then wrote 72 junk records on a differently-shaped site
(schwaben.digital, 31 Jul: image filenames, staff photos, sponsor logos).
A count threshold cannot tell a curated company grid from a footer logo strip.
Structure can.

The central idea
----------------
A portfolio grid, a company directory and an article list all share one
property: they are built from a REPEATING UNIT. Find that unit — a run of
sibling elements sharing a class signature — and you know what the page is,
how many entities it contains, and how to extract them. A footer sponsor strip
is also repeating, so repetition alone is not enough; the unit must also carry
DISTINCT IDENTITY (unique links or unique names) and must not sit in nav/footer
chrome. That combination is what separates the two incidents structurally.

Everything here is deterministic and free — no LLM, no network in the core
functions. The Phase R-3 strategist only adjudicates the verdict this module
produces; it is never load-bearing.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Bump to force a global re-probe of every cached SiteProfile (Phase R-2).
INSPECTOR_VERSION = 1

_NAV_FOOTER_TAGS = {"nav", "footer", "header", "aside"}
# Stem-matched, not \b-terminated on the right: a trailing \b after these
# words would refuse to match their own plurals ("breadcrumbs", "sponsors")
# or compounds ("navbar", "cookie-banner") — found live 3 Aug on
# uni-augsburg.de, where a breadcrumb trail (<ol class="breadcrumbs">) was
# card-detected as a 5-entity "logo grid" ('Universität', 'Organisation',
# 'Einrichtungen', 'Startseite', ...) because \bbreadcrumb\b cannot match
# inside "breadcrumbs" — "breadcrumb" is immediately followed by "s", both
# word characters, so there is no boundary between them at all.
_NAV_FOOTER_HINTS = re.compile(
    r"(?:^|[-_\s])(?:nav|menu|footer|header|breadcrumb|social|partner|sponsor|cookie|banner)",
    re.IGNORECASE,
)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_NAME_CLASS_HINT = re.compile(r"(title|name|heading|label|company)", re.IGNORECASE)

# "12 startups", "43 Unternehmen" — an explicit self-reported total is the most
# trustworthy entity count a page can give us.
_COUNT_TEXT_RE = re.compile(
    r"\b(\d{2,4})\s+(startups?|companies|unternehmen|firmen|portfolio|mitglieder|members)\b",
    re.IGNORECASE,
)


# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ItemFeatures:
    name: Optional[str]
    href: Optional[str]
    text_len: int
    tag_count: int
    has_img: bool
    has_heading: bool
    # Full item text, bounded — Phase R-4's card_structured extraction mode
    # needs the actual per-card content, not just its length. Kept on the
    # frozen dataclass (not re-parsed from the DOM later) so a card's text is
    # captured exactly once, at detection time.
    text: str = ""


@dataclass
class CardGroup:
    signature: str
    parent_path: str
    items: list
    score: float = 0.0
    frac_with_link: float = 0.0
    frac_unique_href: float = 0.0
    frac_unique_name: float = 0.0
    frac_with_img: float = 0.0
    frac_with_heading: float = 0.0
    median_text_len: int = 0
    shape_cv: float = 0.0
    in_nav_or_footer: bool = False
    name_only: bool = False
    frac_headline_names: float = 0.0

    @property
    def n(self) -> int:
        return len(self.items)

    def names(self) -> list:
        return [i.name for i in self.items if i.name]

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "parent_path": self.parent_path,
            "n": self.n,
            "score": round(self.score, 3),
            "frac_with_link": round(self.frac_with_link, 2),
            "frac_unique_href": round(self.frac_unique_href, 2),
            "frac_unique_name": round(self.frac_unique_name, 2),
            "frac_with_img": round(self.frac_with_img, 2),
            "frac_with_heading": round(self.frac_with_heading, 2),
            "median_text_len": self.median_text_len,
            "shape_cv": round(self.shape_cv, 2),
            "in_nav_or_footer": self.in_nav_or_footer,
            "name_only": self.name_only,
            "frac_headline_names": round(self.frac_headline_names, 2),
            "sample_names": self.names()[:8],
        }


@dataclass
class StructuralSignals:
    url: str
    card_groups: list = field(default_factory=list)
    primary_group: Optional[CardGroup] = None
    jsonld_types: dict = field(default_factory=dict)
    jsonld_item_count: int = 0
    alt_count_raw: int = 0
    alt_count_clean: int = 0
    text_len: int = 0
    link_density: float = 0.0
    prose_density: float = 0.0
    self_reported_count: int = 0
    detail_link_pattern: Optional[str] = None
    detail_link_coverage: float = 0.0
    pagination_kind: Optional[str] = None
    load_more_selector: Optional[str] = None
    render_gain: Optional[float] = None
    candidate_entity_count: int = 0

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "candidate_entity_count": self.candidate_entity_count,
            "primary_group": self.primary_group.to_dict() if self.primary_group else None,
            "other_groups": [g.to_dict() for g in self.card_groups[1:]],
            "jsonld_types": self.jsonld_types,
            "jsonld_item_count": self.jsonld_item_count,
            "alt_count_raw": self.alt_count_raw,
            "alt_count_clean": self.alt_count_clean,
            "text_len": self.text_len,
            "link_density": round(self.link_density, 3),
            "prose_density": round(self.prose_density, 3),
            "self_reported_count": self.self_reported_count,
            "detail_link_pattern": self.detail_link_pattern,
            "detail_link_coverage": round(self.detail_link_coverage, 2),
            "pagination_kind": self.pagination_kind,
            "render_gain": self.render_gain,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _median(values: list) -> int:
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    return int(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2)


def _cv(values: list) -> float:
    """Coefficient of variation — how structurally alike a group's items are."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def _in_nav_or_footer(el) -> bool:
    """True if this element sits inside page chrome rather than main content."""
    node = el
    for _ in range(6):
        if node is None or getattr(node, "name", None) is None:
            break
        if node.name in _NAV_FOOTER_TAGS:
            return True
        blob = " ".join(
            [" ".join(node.get("class") or []), node.get("id") or "", node.get("role") or ""]
        )
        if blob.strip() and _NAV_FOOTER_HINTS.search(blob):
            return True
        node = node.parent
    return False


def _element_path(el) -> str:
    parts = []
    node = el
    for _ in range(3):
        if node is None or getattr(node, "name", None) is None:
            break
        cls = ".".join((node.get("class") or [])[:2])
        parts.append(f"{node.name}.{cls}" if cls else node.name)
        node = node.parent
    return " < ".join(parts)


def _item_name(el, is_junk_name) -> Optional[str]:
    """
    Best-effort entity name for one card, most-reliable source first.
    Every candidate goes through the junk filter — the same generic one that
    keeps CMS filenames and staff handles out of the alt harvest.

    Deliberately does NOT also reject headline-shaped text here (tried live
    3 Aug, reverted): filtering at the item level means a real news-feed
    group loses most of its names to the filter (headlines ARE its content),
    which lowers ITS OWN frac_unique_name enough that an unrelated, cleanly-
    short-named decoy group (found live: startbase.de's browser-compatibility
    widget — "Mozilla Firefox", "Google Chrome", "Safari" — nothing to do
    with the page's actual news feed) can outscore it and win as primary
    instead. The group-level frac_headline_names check in _build_group,
    computed from these UNFILTERED names, is what correctly catches a
    headline-heavy group as editorial — see _is_editorial.
    """
    def ok(v):
        v = (v or "").strip()
        if len(v) < 2 or len(v) > 120:
            return None
        return None if is_junk_name(v) else v

    for tag in _HEADING_TAGS:
        h = el.find(tag)
        if h and (v := ok(h.get_text(strip=True))):
            return v
    for node in el.find_all(attrs={"class": _NAME_CLASS_HINT}, limit=3):
        if v := ok(node.get_text(strip=True)):
            return v
    a = el.find("a", title=True)
    if a and (v := ok(a.get("title"))):
        return v
    img = el.find("img", alt=True)
    if img and (v := ok(img.get("alt"))):
        return v
    a = el.find("a")
    if a and (v := ok(a.get_text(strip=True))):
        return v
    txt = el.get_text(" ", strip=True)
    if len(txt) <= 60 and (v := ok(txt)):
        return v
    return None


def _signature(child, sibling_class_counts: Counter) -> str:
    """
    Class signature for a child, keeping ONLY class tokens shared by >=2
    siblings. This is what makes grouping robust with zero per-site knowledge:
    BEM modifiers (`card card--featured`), utility one-offs and generated ids
    appear once and are dropped, while the structural class that defines the
    repeating unit survives.
    """
    shared = sorted(c for c in (child.get("class") or []) if sibling_class_counts[c] >= 2)
    return f"{child.name}.{'.'.join(shared)}" if shared else child.name


def _structural_signature(child) -> str:
    """Fallback for CSS-in-JS / inline-styled grids with no usable classes."""
    kids = [c.name for c in child.find_all(recursive=False) if getattr(c, "name", None)][:3]
    return f"{child.name}>{'>'.join(kids)}" if kids else child.name


# ── Card detection ───────────────────────────────────────────────────────────

def detect_card_groups(soup, cfg: dict, is_junk_name=None) -> list:
    """Find repeating sibling groups that look like entity cards, best first."""
    if is_junk_name is None:
        from ingestion.web_scraper import _is_junk_alt as is_junk_name

    min_items = int(cfg.get("min_group_items", 4))
    w = cfg.get("weights", {})
    name_only_max = int(cfg.get("name_only_text_max_chars", 24))
    name_only_frac = float(cfg.get("name_only_min_fraction", 0.8))

    groups: list = []
    seen_parents = set()

    for parent in soup.find_all(True):
        children = [c for c in parent.find_all(recursive=False) if getattr(c, "name", None)]
        if len(children) < min_items:
            continue
        pid = id(parent)
        if pid in seen_parents:
            continue
        seen_parents.add(pid)

        class_counts = Counter()
        for c in children:
            class_counts.update(set(c.get("class") or []))

        for sig_fn in (lambda c: _signature(c, class_counts), _structural_signature):
            buckets = defaultdict(list)
            for c in children:
                buckets[sig_fn(c)].append(c)
            made = False
            for sig, members in buckets.items():
                if len(members) < min_items:
                    continue
                g = _build_group(sig, parent, members, is_junk_name, name_only_max, name_only_frac)
                _score_group(g, w, min_items)
                groups.append(g)
                made = True
            if made:
                break  # class grouping worked; don't also add structural dupes

    groups.sort(key=lambda g: g.score, reverse=True)
    return groups


def _build_group(sig, parent, members, is_junk_name, name_only_max, name_only_frac) -> CardGroup:
    items = []
    for el in members:
        a = el.find("a", href=True)
        full_text = el.get_text(" ", strip=True)
        items.append(ItemFeatures(
            name=_item_name(el, is_junk_name),
            href=(a.get("href") if a else None),
            text_len=len(full_text),
            tag_count=len(el.find_all(True)),
            has_img=el.find("img") is not None,
            has_heading=any(el.find(t) for t in _HEADING_TAGS),
            text=full_text[:3000],
        ))

    n = len(items)
    hrefs = [i.href for i in items if i.href]
    names = [i.name for i in items if i.name]
    text_lens = [i.text_len for i in items]

    g = CardGroup(signature=sig, parent_path=_element_path(parent), items=items)
    g.frac_with_link = len(hrefs) / n
    g.frac_unique_href = len(set(hrefs)) / n if hrefs else 0.0
    g.frac_unique_name = len(set(x.lower() for x in names)) / n if names else 0.0
    g.frac_with_img = sum(1 for i in items if i.has_img) / n
    g.frac_with_heading = sum(1 for i in items if i.has_heading or i.name) / n
    g.median_text_len = _median(text_lens)
    g.shape_cv = _cv([i.tag_count for i in items])
    g.in_nav_or_footer = _in_nav_or_footer(parent)
    g.name_only = (
        sum(1 for t in text_lens if t <= name_only_max) / n >= name_only_frac
    )
    g.frac_headline_names = (
        sum(1 for x in names if _is_headline_like(x)) / len(names) if names else 0.0
    )
    return g


# A company name and a news headline occupy very different shapes, and the
# difference is universal rather than per-site: "Nouma Autonomy" (2 words,
# 14 chars) vs "Konstanzer MedTech-Startup EVERSION erhält 2,3 Millionen Euro
# Seed-II-Finanzierung" (9 words, 82 chars, contains a comma and digits).
# Without this, a news feed's cards are structurally indistinguishable from a
# portfolio grid's, and every homepage manufactures a phantom entity count.
_SENTENCE_PUNCT = re.compile(r"[?!:;–—]|\.\s")
_MAX_NAME_CHARS = 45
_MAX_NAME_WORDS = 6


def _is_headline_like(name: str) -> bool:
    if not name:
        return False
    if len(name) > _MAX_NAME_CHARS:
        return True
    if len(name.split()) > _MAX_NAME_WORDS:
        return True
    if _SENTENCE_PUNCT.search(name):
        return True
    # _SENTENCE_PUNCT's `\.\s` requires a period FOLLOWED BY more text, so a
    # short standalone sentence that IS the whole card text -- nothing after
    # the closing period -- never matched it. Found live 14 Aug 2026: a
    # password-requirements checklist on munich-startup.de's own startup-
    # signup form ("Enthält einen Großbuchstaben.", "Mindestens 8 Zeichen
    # lang.", one hint per card) scored as a logo grid and got harvested as
    # six "startup names," each spawning a possible_duplicate review against
    # a real company whose name happened to share a token. Real proper-noun
    # names don't end in a literal period; multi-word text that does is a
    # complete declarative sentence. Single-token names that DO end in a
    # period as part of an abbreviation or domain (K.I.T., titanspear.ai)
    # are exempt via the space requirement -- they have no space to trigger
    # this branch.
    return " " in name and name.endswith(".")


def _score_group(g: CardGroup, w: dict, min_items: int) -> None:
    """
    Score a group on how much it looks like a real entity directory.

    `max(frac_unique_href, frac_unique_name)` carries the most weight and the
    max() is deliberate: a logo wall whose items are plain <img> with no anchor
    scores zero on uniqueness-by-link but ~1.0 on uniqueness-by-name, and it is
    exactly the shape we must not miss (zollhof). Conversely a nav bar repeats
    structure but its items are neither uniquely named nor uniquely linked in a
    content sense, and it also takes the nav/footer penalty.
    """
    identity = max(g.frac_unique_href, g.frac_unique_name)
    score = (
        w.get("unique_identity", 0.30) * identity
        + w.get("has_link", 0.20) * g.frac_with_link
        + w.get("has_heading", 0.15) * g.frac_with_heading
        + w.get("homogeneity", 0.15) * (1 - min(1.0, g.shape_cv))
        + w.get("group_size", 0.10) * min(1.0, g.n / max(1, min_items))
        + w.get("has_image", 0.10) * g.frac_with_img
    )
    if g.in_nav_or_footer:
        # A CAP, not a subtraction. Found live 3 Aug: a breadcrumb trail
        # (<ol class="breadcrumbs">) is small, fully linked, all-distinct
        # hrefs, structurally homogeneous — it scores ~0.90 on every OTHER
        # signal, so a flat -0.25 still cleared the 0.55 threshold. Page
        # chrome (nav/menu/footer/breadcrumb/cookie/banner — and in
        # practice, for THIS pipeline's purpose, "partner"/"sponsor"
        # sections too: schwaben.digital's and cdtm.de's both turned out to
        # be law firms/banks/universities, not startups) is never a
        # legitimate entity directory regardless of how grid-like it looks,
        # so it must never be able to outscore the threshold at all.
        score = min(score, w.get("nav_footer_cap", 0.15))
    if g.median_text_len < 3 and identity < 0.5:
        # Structurally repeating but carries neither text nor distinct identity
        # — decorative chrome (icon strips, spacers), never an entity list.
        score -= w.get("empty_text_penalty", 0.30)
    g.score = max(0.0, score)


# ── Other signals ────────────────────────────────────────────────────────────

def _jsonld(soup) -> tuple:
    """Return (type_counts, best_item_count). JSON-LD is the highest-precision
    entity count a page can offer, when it publishes one."""
    types, item_count = Counter(), 0
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in _walk_jsonld(data):
            t = node.get("@type")
            if isinstance(t, list):
                t = t[0] if t else None
            if t:
                types[str(t)] += 1
            if t in ("ItemList", "CollectionPage"):
                n = node.get("numberOfItems")
                elems = node.get("itemListElement") or []
                item_count = max(item_count, int(n) if isinstance(n, int) else len(elems))
    return dict(types), item_count


def _walk_jsonld(node, depth: int = 0):
    if depth > 6:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_jsonld(v, depth + 1)


def _detail_link_pattern(group: CardGroup, base_url: str, min_cov: float) -> tuple:
    """Longest common path prefix across a group's item links, + coverage."""
    if not group:
        return None, 0.0
    paths, seen = [], set()
    for i in group.items:
        if not i.href:
            continue
        try:
            p = urlparse(urljoin(base_url, i.href))
        except Exception:
            continue
        if p.netloc and p.netloc != urlparse(base_url).netloc:
            continue
        segs = [s for s in p.path.split("/") if s]
        if segs and p.path not in seen:
            seen.add(p.path)
            paths.append(segs)
    if len(paths) < 2:
        return None, 0.0

    prefix = []
    for parts in zip(*paths):
        if len(set(parts)) == 1:
            prefix.append(parts[0])
        else:
            break
    coverage = len(paths) / group.n
    if not prefix or coverage < min_cov:
        return None, coverage
    return "/" + "/".join(prefix) + "/*", coverage


def _pagination(soup) -> tuple:
    """(kind, selector_hint) — presence only; clicking happens in probe_url."""
    from ingestion.web_scraper import _LOAD_MORE_PATTERNS
    for el in soup.find_all(["button", "a"]):
        label = el.get_text(" ", strip=True).lower()
        if label and any(p.lower() in label for p in _LOAD_MORE_PATTERNS):
            return "load_more", label
    if soup.find("a", rel="next"):
        return "rel_next", None
    nums = [
        a for a in soup.find_all("a")
        if a.get_text(strip=True).isdigit() and len(a.get_text(strip=True)) <= 3
    ]
    if len(nums) >= 2:
        return "numbered", None
    return None, None


# ── Public API ───────────────────────────────────────────────────────────────

def probe_html(html: str, url: str, cfg: dict = None) -> StructuralSignals:
    """Compute every structural signal for one page. Deterministic, no I/O."""
    from config.tuning_loader import get_inspector_config
    from ingestion.web_scraper import _is_junk_alt

    cfg = cfg or get_inspector_config()
    sig = StructuralSignals(url=url)
    if not html:
        return sig

    soup = BeautifulSoup(html, "html.parser")

    groups = detect_card_groups(soup, cfg, _is_junk_alt)
    top_n = int(cfg.get("top_groups_reported", 5))
    sig.card_groups = groups[:top_n]
    threshold = float(cfg.get("card_score_threshold", 0.55))
    if groups and groups[0].score >= threshold:
        sig.primary_group = groups[0]

    sig.jsonld_types, sig.jsonld_item_count = _jsonld(soup)

    alts, seen = [], set()
    for img in soup.find_all("img"):
        a = (img.get("alt") or "").strip()
        k = a.lower()
        if len(a) < 2 or k in seen:
            continue
        seen.add(k)
        alts.append(a)
    sig.alt_count_raw = len(alts)
    sig.alt_count_clean = sum(1 for a in alts if not _is_junk_alt(a))

    text = soup.get_text(" ", strip=True)
    sig.text_len = len(text)
    link_chars = sum(len(a.get_text(" ", strip=True)) for a in soup.find_all("a"))
    sig.link_density = link_chars / sig.text_len if sig.text_len else 0.0
    tags = len(soup.find_all(True)) or 1
    sig.prose_density = sig.text_len / tags

    if m := _COUNT_TEXT_RE.search(text):
        sig.self_reported_count = int(m.group(1))

    sig.detail_link_pattern, sig.detail_link_coverage = _detail_link_pattern(
        sig.primary_group, url, float(cfg.get("detail_link_min_coverage", 0.6))
    )
    sig.pagination_kind, sig.load_more_selector = _pagination(soup)

    # Entity count, most trustworthy source first. JSON-LD is publisher-declared;
    # a detected group is measured; the clean alt count is a last resort and only
    # when a grid was actually confirmed — never on its own, which is precisely
    # the mistake that let schwaben's sponsor logos become records.
    if sig.jsonld_item_count >= int(cfg.get("min_group_items", 4)):
        sig.candidate_entity_count = sig.jsonld_item_count
    elif sig.primary_group:
        sig.candidate_entity_count = sig.primary_group.n
    else:
        sig.candidate_entity_count = 0

    return sig


def derive_strategy_deterministic(sig: StructuralSignals, cfg: dict = None):
    """
    Turn structural signals into a strategy. Runs ALWAYS and first; the Phase
    R-3 strategist only adjudicates this verdict and can never replace it when
    Ollama is unavailable.

    Ladder, most-certain evidence first. The final rung reproduces today's
    behaviour exactly, so an unrecognised page is never worse off than before.
    """
    from ingestion.strategy import PageStrategy
    from config.tuning_loader import get_inspector_config

    cfg = cfg or get_inspector_config()
    g = sig.primary_group
    needs_render = bool(sig.render_gain and sig.render_gain >= float(cfg.get("render_gain_threshold", 2.0)))
    paginate = sig.pagination_kind is not None
    base = dict(
        needs_render=needs_render,
        paginate=paginate,
        load_more_selector=sig.load_more_selector,
        detail_link_pattern=sig.detail_link_pattern,
        source="deterministic",
    )

    # A repeating group whose detail links point at editorial content is a news
    # or events feed, not a company directory. Universal URL convention, not a
    # per-site rule — and it matters because an entity expectation on an events
    # list would make the recall audit chase a shortfall that cannot exist.
    if g is not None and _is_editorial(sig):
        return PageStrategy(
            page_shape="article_feed", text_extraction="main_prose",
            chunking="sliding_window", expected_entity_count=0,
            confidence="medium",
            reason=f"repeating group of {g.n} linking to {sig.detail_link_pattern} — editorial feed, not an entity directory",
            **base,
        )

    # Publisher-declared item count: the most precise signal a page can give.
    if sig.jsonld_item_count >= int(cfg.get("min_group_items", 4)):
        return PageStrategy(
            page_shape="card_directory", text_extraction="card_structured",
            chunking="per_card", bypass_candidate_filter=True,
            expected_entity_count=sig.jsonld_item_count, confidence="high",
            reason=f"JSON-LD ItemList declares {sig.jsonld_item_count} items",
            **base,
        )

    if g is not None and g.name_only:
        return PageStrategy(
            page_shape="logo_grid", text_extraction="alt_harvest",
            chunking="name_batch", bypass_candidate_filter=True,
            follow_detail_links=bool(sig.detail_link_pattern),
            expected_entity_count=g.n, confidence="high",
            reason=f"{g.n} name-only cards ({g.signature}), score {g.score:.2f}",
            **base,
        )

    if g is not None:
        return PageStrategy(
            page_shape="card_directory", text_extraction="card_structured",
            chunking="per_card", bypass_candidate_filter=True,
            expected_entity_count=g.n, confidence="high",
            reason=f"{g.n} content cards ({g.signature}), median text {g.median_text_len} chars",
            **base,
        )

    if sig.text_len and sig.link_density <= float(cfg.get("prose_link_density_max", 0.45)):
        return PageStrategy(
            page_shape="prose_listing", text_extraction="main_prose",
            chunking="sliding_window", expected_entity_count=0,
            confidence="medium",
            reason=f"no repeating group; prose-shaped (link density {sig.link_density:.2f})",
            **base,
        )

    # Nothing recognised — today's exact behaviour, and no expectation, so the
    # recall audit stays silent rather than inventing a target.
    return PageStrategy(
        page_shape="unknown", text_extraction="full_text",
        chunking="sliding_window", expected_entity_count=0, confidence="low",
        reason="no structural signal; using the default path",
        **base,
    )


# Detail-URL segments that mean "editorial content", not "an entity we track".
# Web-wide conventions (EN + DE), deliberately not tied to any one site.
_EDITORIAL_SEGMENTS = frozenset({
    "event", "events", "veranstaltung", "veranstaltungen", "termine", "termin",
    "news", "neuigkeiten", "blog", "article", "articles", "artikel", "post",
    "posts", "presse", "press", "magazin", "magazine", "stories", "story",
    "aktuelles", "jobs", "karriere", "career", "careers", "kurse", "webinar",
})


def _is_editorial(sig: StructuralSignals) -> bool:
    """
    True when a detected group is an editorial feed rather than an entity list.
    Any one tell is sufficient — they fail independently across real sites:

      1. Detail links under an editorial path. Segments are tokenised on -/_
         because real CMSs emit compound segments: sce.de uses
         `/news-details/*`, which an exact-segment match misses entirely.
      2. Item names shaped like headlines rather than company names. This is
         the tell that catches a news feed with no distinguishing URL at all
         (startbase.de's 20 cards link nowhere useful, but every "name" is a
         full sentence).
      3. Names repeating heavily — a recurring-events widget lists the same
         title several times; a real directory never does.
    """
    if sig.detail_link_pattern:
        tokens = set()
        for seg in sig.detail_link_pattern.split("/"):
            if seg and seg != "*":
                tokens.update(re.split(r"[-_]", seg.lower()))
        if tokens & _EDITORIAL_SEGMENTS:
            return True

    g = sig.primary_group
    if g is None:
        return False
    if not g.name_only and g.frac_headline_names >= 0.6:
        return True
    if not g.name_only and g.n >= 4 and g.frac_unique_name < 0.7:
        return True
    return False


def count_entities(html: str, url: str = "", cfg: dict = None) -> int:
    """Hot path: how many entities this page structurally appears to contain.
    Recomputed per page per run — never cached (a cached count cannot notice a
    site redesign, which is the whole safety property of the recall audit)."""
    return probe_html(html, url, cfg).candidate_entity_count


def harvest_entities(html: str, url: str = "", cfg: dict = None) -> tuple:
    """
    (names, per-card (name, text) blocks) from the primary group only —
    Phase R-4's card_structured/alt_harvest extraction modes consume this
    directly instead of the page's full text.
    """
    sig = probe_html(html, url, cfg)
    if not sig.primary_group:
        return [], []
    g = sig.primary_group
    names = [n for n in g.names()]
    blocks = [(i.name, i.text) for i in g.items if i.text]
    return names, blocks


async def probe_url(url: str, *, client=None, force_render: bool = False, cfg: dict = None):
    """
    Fetch a page BOTH ways (plain static + headless-rendered) and return one
    StructuralSignals with render_gain filled in — the only place that field
    is computed, since it requires two independent fetches (I/O), unlike
    every other signal in this module.

    Deliberately does NOT reuse WebScraper._fetch_page's own static-fetch
    path for the static half: that method auto-escalates internally on a
    char-length heuristic, which would silently hand back rendered HTML and
    make the two samples non-independent. The comparison is measured on
    ENTITY COUNT, not character length — a page can be char-heavy and still
    entity-empty (boilerplate), which is exactly what a naive char-gain
    metric would get wrong.

    force_render=True skips the static attempt and renders directly — used
    when a profile is already known to need rendering (a pinned or previously
    "active" source), so re-probing doesn't waste a doomed static fetch.
    """
    import httpx
    from ingestion.web_scraper import WebScraper
    from config.tuning_loader import get_inspector_config

    cfg = cfg or get_inspector_config()
    scraper = WebScraper()
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    try:
        static_html = ""
        if not force_render:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                    static_html = resp.text
            except Exception as exc:
                logger.debug(f"[Inspector] static fetch failed for {url}: {exc}")

        static_sig = probe_html(static_html, url, cfg) if static_html else StructuralSignals(url=url)

        # Render when forced, or when the static pass found no candidate
        # entities at all — the same trigger _fetch_page uses (thin content),
        # but keyed on entities rather than raw char length so a verbose,
        # entity-free shell still escalates.
        should_render = force_render or static_sig.candidate_entity_count == 0
        rendered_html = ""
        if should_render:
            try:
                rendered_html = await scraper._fetch_page(client, url, force_render=True)
            except Exception as exc:
                logger.debug(f"[Inspector] render fetch failed for {url}: {exc}")

        if not rendered_html:
            return static_sig

        rendered_sig = probe_html(rendered_html, url, cfg)
        if static_sig.candidate_entity_count > 0:
            rendered_sig.render_gain = round(
                rendered_sig.candidate_entity_count / static_sig.candidate_entity_count, 1
            )
        elif rendered_sig.candidate_entity_count > 0:
            # Static found nothing at all but rendering revealed entities —
            # an unambiguous "must render" case. 999.0 rather than inf: this
            # gets persisted into a JSON column, and json.dumps rejects inf.
            rendered_sig.render_gain = 999.0
        else:
            rendered_sig.render_gain = 0.0
        return rendered_sig
    finally:
        if owns_client:
            await client.aclose()
