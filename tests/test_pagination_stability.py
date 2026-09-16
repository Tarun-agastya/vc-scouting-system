"""Paginated list endpoints must define a TOTAL order, not just a sort column.

The bug this guards against (found 16 Sep 2026): every sort column offered by
Browse and by the Regional register is non-unique — enrichment_score,
extracted_at, triage_tier and the compound priority score all have large tie
groups — and Postgres may order rows *within* a tie differently per query.
Combined with LIMIT/OFFSET that produced silent data loss: paging through
Browse showed some startups twice and others never at all (measured on the
live 2,985-row table: 14 duplicates in the first 150 rows of sort=score, 11
of sort=priority, 7 of sort=extracted_at, 3 of the Regional sort=tier).

Two halves, and they are NOT equally strong — worth knowing before trusting
them:

  * `test_*_order_by_ends_with_a_unique_column` is the real guard. It sniffs
    the SQL actually sent to Postgres and asserts the final ORDER BY term is
    the primary key. Verified to FAIL when the tiebreaker is removed.

  * `test_*_paging_shows_every_row_exactly_once` is an end-to-end smoke
    check. It does NOT reproduce the original bug and passes without the fix:
    the instability needs a table large enough for the planner to choose a
    top-N heapsort, whose tie order varies with the LIMIT+OFFSET it is given,
    and a dozen seeded rows get a plain sort that happens to come back
    identically every time. Kept because it exercises the real endpoint end
    to end, not because it can catch a regression on its own.
"""
import asyncio

import pytest
from sqlalchemy import event

from api.routes import regional as R
from api.routes import scout as S
from database.connection import SessionLocal, engine
from database.models import RegionalCompany, Startup

SPREFIX = "PYTEST-PAGE"
RPREFIX = "PYTEST-RPAGE"
N = 12          # rows in one tie group
LIMIT = 5       # deliberately not a divisor of N, so pages cut through ties


def _run(coro):
    return asyncio.run(coro)


def _walk(call, key):
    """Page through `call(offset)` and return (all ids in order, duplicates)."""
    seen, order = {}, []
    for offset in range(0, N * 3, LIMIT):
        page = call(offset)
        rows = page[key]
        if not rows:
            break
        for row in rows:
            order.append(row["id"])
            seen.setdefault(row["id"], []).append(offset)
    return order, {k: v for k, v in seen.items() if len(v) > 1}


# ── Browse (/scout/list) ──────────────────────────────────────────────────────

@pytest.fixture
def seeded_startups():
    def purge():
        db = SessionLocal()
        try:
            db.query(Startup).filter(Startup.name.like(f"{SPREFIX}%")).delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()

    purge()
    db = SessionLocal()
    try:
        # Identical enrichment_score AND identical extracted_at: one big tie
        # group under every sort option Browse offers.
        from datetime import datetime
        when = datetime(2026, 1, 1, 12, 0, 0)
        db.add_all([
            Startup(name=f"{SPREFIX} {i:02d}", normalized_name=f"{SPREFIX.lower()} {i:02d}",
                    enrichment_score=50, extracted_at=when, created_at=when,
                    source="pytest", source_url="https://pytest/pagination",
                    industry="GreenTech", business_model="B2B", is_gmbh=True)
            for i in range(N)
        ])
        db.commit()
    finally:
        db.close()
    yield
    purge()


@pytest.mark.parametrize("sort", ["score", "extracted_at", "created_at", "name", "priority"])
def test_browse_paging_shows_every_row_exactly_once(seeded_startups, sort):
    db = SessionLocal()
    try:
        def call(offset):
            return _run(S.list_startups(q=SPREFIX, sort=sort, order="desc",
                                        limit=LIMIT, offset=offset, db=db))
        order, dupes = _walk(call, "startups")
    finally:
        db.close()

    assert not dupes, f"sort={sort}: rows returned on two different pages: {dupes}"
    assert len(set(order)) == N, (
        f"sort={sort}: paged walk reached {len(set(order))} of {N} seeded rows — "
        "an unstable ORDER BY makes the missing ones unreachable")


# ── Regional register (/regional) ─────────────────────────────────────────────

@pytest.fixture
def seeded_regional():
    def purge():
        db = SessionLocal()
        try:
            db.query(RegionalCompany).filter(
                RegionalCompany.name.like(f"{RPREFIX}%")).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    purge()
    db = SessionLocal()
    try:
        # Same tier, same distance, same headcount — a tie under every sort.
        db.add_all([
            RegionalCompany(name=f"{RPREFIX} {i:02d}",
                            normalized_name=f"{RPREFIX.lower()} {i:02d}",
                            city="Memmingen", distance_km=5.0, in_radius=True,
                            employees=200, branche="Maschinenbau", triage_tier=1,
                            source="pytest")
            for i in range(N)
        ])
        db.commit()
    finally:
        db.close()
    yield
    purge()


@pytest.mark.parametrize("sort", ["distance", "employees", "tier", "name"])
def test_regional_paging_shows_every_row_exactly_once(seeded_regional, sort):
    db = SessionLocal()
    try:
        def call(offset):
            return _run(R.list_regional(q=RPREFIX, sort=sort, limit=LIMIT,
                                        offset=offset, db=db))
        order, dupes = _walk(call, "companies")
    finally:
        db.close()

    assert not dupes, f"sort={sort}: companies returned on two different pages: {dupes}"
    assert len(set(order)) == N, (
        f"sort={sort}: paged walk reached {len(set(order))} of {N} seeded rows")


# ── The actual guard: sniff the SQL ───────────────────────────────────────────

class _OrderBySniffer:
    """
    Collect the ORDER BY clause of every SELECT the endpoint emits.

    Reads the statement as handed to the driver rather than re-deriving it, so
    it cannot drift from what Postgres is really asked to do — which is the
    whole point, since the defect was invisible in the Python and only visible
    in the emitted SQL.
    """

    def __init__(self):
        self.clauses = []

    def _hook(self, conn, cursor, statement, params, context, executemany):
        low = statement.lower()
        at = low.rfind(" order by ")
        if at == -1:
            return
        clause = statement[at + len(" order by "):]
        for stop in (" limit ", " offset "):
            cut = clause.lower().find(stop)
            if cut != -1:
                clause = clause[:cut]
        self.clauses.append(" ".join(clause.split()))

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False

    def final_terms(self):
        """The last ORDER BY term of each collected clause, normalised."""
        out = []
        for clause in self.clauses:
            last = clause.split(",")[-1].strip().lower()
            for suffix in (" asc", " desc", " nulls last", " nulls first"):
                while last.endswith(suffix):
                    last = last[: -len(suffix)].strip()
            out.append(last)
        return out


@pytest.mark.parametrize("sort", ["score", "extracted_at", "created_at", "name", "priority"])
def test_browse_order_by_ends_with_a_unique_column(seeded_startups, sort):
    db = SessionLocal()
    try:
        with _OrderBySniffer() as sniff:
            _run(S.list_startups(q=SPREFIX, sort=sort, order="desc", limit=LIMIT,
                                 offset=0, db=db))
    finally:
        db.close()

    terms = sniff.final_terms()
    assert terms, f"sort={sort}: no ORDER BY reached the database at all"
    assert all(t == "startups.id" for t in terms), (
        f"sort={sort}: ORDER BY must end with the primary key so ties resolve "
        f"identically on every page; got final terms {terms}")


@pytest.mark.parametrize("sort", ["distance", "employees", "tier", "name"])
def test_regional_order_by_ends_with_a_unique_column(seeded_regional, sort):
    db = SessionLocal()
    try:
        with _OrderBySniffer() as sniff:
            _run(R.list_regional(q=RPREFIX, sort=sort, limit=LIMIT, offset=0, db=db))
    finally:
        db.close()

    terms = sniff.final_terms()
    assert terms, f"sort={sort}: no ORDER BY reached the database at all"
    assert all(t == "regional_companies.id" for t in terms), (
        f"sort={sort}: ORDER BY must end with the primary key; got {terms}")
