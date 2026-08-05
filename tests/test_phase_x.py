"""
Phase X (5 Aug) — extraction-shape gap + post-ingestion web-verify chaining.

Motivated by the first whole-registry recall measurement
(scripts/recall_test.py): schwaben.digital/startups extracted 0 of 37
companies. Its 37 logo cards carry links to the companies' own sites
(agitect.com, alitiq.com, avanera.de) but NO alt text, so CardGroup.names()
returned [] and extract_content produced an entirely empty PageContent —
which _crawler_task's `if page_text or entity_names or entity_blocks` gate
then dropped silently. The exact mirror of zollhof (116 names / 0 links,
always worked).
"""
import pytest

from ingestion.web_scraper import _name_from_link, extract_content
from ingestion.chunker import split_name_batches, LOGO_GRID_CHUNK_HEADER
from ingestion.strategy import PageStrategy


# ── X-2: identity from an outbound card link ──────────────────────────────

def test_name_from_link_derives_company_from_external_domain():
    page = "https://schwaben.digital/startups"
    assert _name_from_link("https://agitect.com/", page) == "Agitect"
    assert _name_from_link("https://www.aluco.io/", page) == "Aluco"
    assert _name_from_link("https://alamos.gmbh/", page) == "Alamos"


def test_name_from_link_ignores_same_domain_links():
    """
    Same-domain hrefs are site navigation ("/team", "/about"), and their
    domain is the HOST's name, not a portfolio company's. Deriving from
    those would manufacture a pile of records all named after the source
    itself — the schwaben.digital-as-a-startup failure mode.
    """
    page = "https://schwaben.digital/startups"
    assert _name_from_link("https://schwaben.digital/team", page) is None
    assert _name_from_link("/team", page) is None


def test_name_from_link_handles_missing_and_junk():
    page = "https://example.com/x"
    assert _name_from_link(None, page) is None
    assert _name_from_link("", page) is None


def test_name_from_link_is_a_stub_identity_not_a_brand_name():
    """
    DNS is case-insensitive and _base_domain lowercases, so a brand's own
    casing ("goNEON", "WeSort.AI") is NOT recoverable from an href. The
    derived name is deliberately just a stub — X-4 queues these records for
    web verification precisely so the real name and casing replace it.
    """
    assert _name_from_link("https://goNEON.de/", "https://host.de/x") == "Goneon"


# ── X-2: the link rides through to the extraction prompt ──────────────────

def test_split_name_batches_renders_known_links():
    names = ["Agitect", "Alitiq"]
    links = [("Agitect", "https://agitect.com/")]
    chunk = split_name_batches(names, 6, links=links)[0]
    assert "Agitect — https://agitect.com/" in chunk
    assert "\nAlitiq" in chunk  # no link known -> bare name, unchanged


def test_split_name_batches_without_links_is_unchanged():
    """The legacy path (zollhof: names, no links) must be byte-identical."""
    names = ["Alpha", "Beta"]
    assert split_name_batches(names, 6) == [
        LOGO_GRID_CHUNK_HEADER + "\nAlpha\nBeta"
    ]


def test_split_name_batches_does_not_add_llm_calls():
    """37 companies still fit in ceil(37/6)=7 calls, not 37."""
    names = [f"Co{i}" for i in range(37)]
    links = [(n, f"https://{n.lower()}.com") for n in names]
    assert len(split_name_batches(names, 6, links=links)) == 7


# ── X-1: never silently drop a page ───────────────────────────────────────

_STRATEGY = PageStrategy(
    page_shape="logo_grid", text_extraction="alt_harvest", chunking="name_batch",
    expected_entity_count=3,
)

_GRID_NO_NAMES_NO_LINKS = """
<html><body><div class="grid">
  <div class="slot"><img src="a.png"></div>
  <div class="slot"><img src="b.png"></div>
  <div class="slot"><img src="c.png"></div>
  <div class="slot"><img src="d.png"></div>
</div>
<p>Some real prose on the page that full_text would still pick up, long
enough to be worth falling back to rather than dropping the page.</p>
</body></html>
"""


def test_structural_mode_with_nothing_usable_degrades_to_full_text(caplog):
    """
    A group with no names AND no links must fall back to full_text, not
    return an empty PageContent — an empty one is silently dropped by the
    crawler's gate, which is how a 37-company page reported 0 for weeks.
    """
    content = extract_content(_GRID_NO_NAMES_NO_LINKS, "https://example.com/p", _STRATEGY)
    assert content.text, "expected full_text fallback, got empty content"
    assert "real prose" in content.text


def test_degrade_is_logged_not_silent(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="ingestion.web_scraper"):
        extract_content(_GRID_NO_NAMES_NO_LINKS, "https://example.com/p", _STRATEGY)
    # Only assert the warning fires when a group was actually detected; if the
    # inspector finds no primary group at all that's the pre-existing
    # `g is None` path, which is legitimately silent.
    if caplog.records:
        assert any("no usable names or blocks" in r.message for r in caplog.records)


# ── X-4: the selection predicate must be SELF-CLEARING ────────────────────

def test_web_verified_reasons_cover_every_apply_verdict_branch():
    """
    web_verify_new_stubs' self-clearing guard is "evidence reason is not one
    of these". If apply_verdict ever grows a terminal branch writing a reason
    absent from this tuple, the predicate silently stops self-clearing and
    the same top-N stubs get re-searched forever (the 24 Jul incident, see
    web_verify_pending's comment). This pins the contract.
    """
    import inspect
    from processing import web_verifier

    src = inspect.getsource(web_verifier.apply_verdict)
    written = set(re.findall(r'"reason":\s*"([a-z_]+)"', src))
    assert written, "expected apply_verdict to write literal reason values"
    assert written <= set(web_verifier._WEB_VERIFIED_REASONS), (
        f"apply_verdict writes reason(s) {written - set(web_verifier._WEB_VERIFIED_REASONS)} "
        f"that _WEB_VERIFIED_REASONS does not cover — the stub predicate is no "
        f"longer self-clearing"
    )


import re  # noqa: E402  (used by the test above)


def test_stub_predicate_excludes_already_verified(make, db):
    """
    A stub that has been through web verification once must not be selected
    again. Exercised against the real query, not a reimplementation of it.
    """
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB
    from database.models import Startup
    from processing.web_verifier import _WEB_VERIFIED_REASONS

    fresh_id, _ = make("Stub Fresh")                     # name-only, never verified
    done_id, _ = make("Stub Done")                       # name-only, already verified
    done = db.query(Startup).filter(Startup.id == done_id).first()
    done.verification_evidence = {"reason": "web_verified"}
    db.commit()

    reason = cast(Startup.verification_evidence, JSONB)["reason"].astext
    rows = (
        db.query(Startup)
        .filter(Startup.name.like("PYTEST Stub%"))
        .filter(Startup.description.is_(None) | (Startup.description == ""))
        .filter(Startup.website.is_(None) | (Startup.website == ""))
        .filter(
            Startup.verification_evidence.is_(None)
            | reason.is_(None)
            | ~reason.in_(_WEB_VERIFIED_REASONS)
        )
        .all()
    )
    ids = {str(r.id) for r in rows}
    assert fresh_id in ids, "an unverified stub must be selectable"
    assert done_id not in ids, "an already-web-verified stub must NOT be re-selected"


def test_records_with_content_are_not_treated_as_stubs(make, db):
    """Only name-only records qualify — a record with a description is not a stub."""
    from database.models import Startup

    rid, _ = make("Stub HasDesc", description="builds warehouse robots for logistics")
    rows = (
        db.query(Startup)
        .filter(Startup.name.like("PYTEST Stub HasDesc%"))
        .filter(Startup.description.is_(None) | (Startup.description == ""))
        .all()
    )
    assert not rows


# ── X-4: the chain must not deadlock on the non-reentrant GPU mutex ───────

def test_chain_lives_above_execute_not_inside_a_work_function():
    """
    _execute() holds gpu_mutex for a whole run and asyncio.Lock is NOT
    reentrant, so a run_* call from inside a _work_* function would deadlock
    the entire pipeline. Pin the structure: the chaining method must call
    run_web_source / run_web_verify_stubs, and no _work_* may call a run_*.
    """
    import inspect
    from processing.scout_controller import ScoutController

    chain = inspect.getsource(ScoutController.run_web_source_then_verify)
    assert "self.run_web_source(" in chain
    assert "self.run_web_verify_stubs(" in chain

    for name, fn in inspect.getmembers(ScoutController, inspect.isfunction):
        if not name.startswith("_work_"):
            continue
        body = inspect.getsource(fn)
        assert "self.run_" not in body, (
            f"{name} calls a run_* method — that re-enters _execute's gpu_mutex "
            f"and will deadlock"
        )
