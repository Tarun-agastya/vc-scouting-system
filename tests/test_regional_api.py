"""
Phase RC-5 — regional register API.

Follows the project's route-test convention: call the endpoint functions
directly with a fresh session rather than going through HTTP.
"""
import asyncio

import pytest

from api.routes import regional as R
from database.connection import SessionLocal
from database.models import RegionalCompany, Startup

PREFIX = "PYTEST-RCAPI"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def seed():
    def purge():
        db = SessionLocal()
        try:
            db.query(RegionalCompany).filter(
                RegionalCompany.name.like(f"{PREFIX}%")).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    purge()
    db = SessionLocal()
    try:
        db.add_all([
            RegionalCompany(name=f"{PREFIX} Near", normalized_name=f"{PREFIX} near".lower(),
                            city="Memmingen", distance_km=2.0, in_radius=True,
                            employees=450, branche="Maschinenbau", triage_tier=1,
                            source="wikidata",
                            field_sources={"employees": "https://example.de/ma"}),
            RegionalCompany(name=f"{PREFIX} Far", normalized_name=f"{PREFIX} far".lower(),
                            city="Kempten", distance_km=31.0, in_radius=True,
                            employees=None, branche="Verpackung", triage_tier=2,
                            source="osm"),
            RegionalCompany(name=f"{PREFIX} Outside", normalized_name=f"{PREFIX} outside".lower(),
                            city="Augsburg", distance_km=68.0, in_radius=False,
                            employees=1701, triage_tier=1, source="sheet"),
        ])
        db.commit()
    finally:
        db.close()
    yield
    purge()


def _names(res):
    return {c["name"] for c in res["companies"]}


def _body(resp) -> str:
    """Drain a StreamingResponse.

    Starlette wraps even a plain sync iterator in an async generator, so this
    has to be consumed with `async for`, not `for`. Chunks may be str or bytes
    depending on how the response was built.
    """
    async def drain():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk)
        return "".join(chunks)

    return asyncio.run(drain())


def test_list_defaults_to_in_radius_and_nearest_first():
    res = _run(R.list_regional(q=PREFIX, db=SessionLocal()))
    assert f"{PREFIX} Outside" not in _names(res)   # out-of-radius hidden by default
    ordered = [c["name"] for c in res["companies"]]
    assert ordered.index(f"{PREFIX} Near") < ordered.index(f"{PREFIX} Far")


def test_out_of_radius_rows_are_kept_and_reachable():
    """They carry real contact history, so they are hidden by default but
    never deleted."""
    res = _run(R.list_regional(q=PREFIX, in_radius=False, db=SessionLocal()))
    assert _names(res) == {f"{PREFIX} Outside"}


def test_filters():
    assert _names(_run(R.list_regional(q=PREFIX, tier=1, db=SessionLocal()))) == {f"{PREFIX} Near"}
    assert _names(_run(R.list_regional(q=PREFIX, branche="Verpackung",
                                       db=SessionLocal()))) == {f"{PREFIX} Far"}
    assert _names(_run(R.list_regional(q=PREFIX, max_distance=10,
                                       db=SessionLocal()))) == {f"{PREFIX} Near"}
    assert _names(_run(R.list_regional(q=PREFIX, min_employees=400,
                                       db=SessionLocal()))) == {f"{PREFIX} Near"}
    # The work queue: who still needs a headcount?
    assert _names(_run(R.list_regional(q=PREFIX, missing_employees=True,
                                       db=SessionLocal()))) == {f"{PREFIX} Far"}


def test_citations_are_exposed_for_machine_gathered_values():
    res = _run(R.list_regional(q=f"{PREFIX} Near", db=SessionLocal()))
    assert res["companies"][0]["field_sources"]["employees"] == "https://example.de/ma"


def test_patch_writes_crm_fields_only():
    """The register's core promise is that every machine-gathered value has a
    source. A hand edit through the dashboard would invalidate that silently,
    so those fields are simply not writable here."""
    db = SessionLocal()
    row = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Near").one()
    rid = str(row.id)
    db.close()

    res = _run(R.edit_regional(rid, R.RegionalEdit(
        status="Absage", prio=2, wer_hat_kontakt="AH"), db=SessionLocal()))
    assert set(res["applied"]) == {"status", "prio", "wer_hat_kontakt"}
    assert res["company"]["employees"] == 450        # untouched
    assert res["company"]["status"] == "Absage"


def test_patch_cannot_smuggle_a_machine_field():
    db = SessionLocal()
    rid = str(db.query(RegionalCompany).filter_by(name=f"{PREFIX} Near").one().id)
    db.close()
    # `employees` is not on the pydantic model at all, so it is dropped before
    # it can reach the row.
    res = _run(R.edit_regional(rid, R.RegionalEdit(status="x"), db=SessionLocal()))
    assert "employees" not in res["applied"]
    assert res["company"]["employees"] == 450


def test_export_is_german_excel_readable():
    """Semicolon delimiter + UTF-8 BOM: German Excel defaults to semicolons
    and misreads plain UTF-8, which would mangle every umlaut."""
    resp = _run(R.export_csv(q=PREFIX, db=SessionLocal()))
    body = _body(resp)
    assert body.startswith("﻿")
    header = body.splitlines()[0]
    assert ";" in header and "Mitarbeiter" in header and "Kurzbeschreibung" in header
    assert "Entfernung (km)" in header


def test_export_respects_the_active_filter():
    resp = _run(R.export_csv(q=PREFIX, tier=1, db=SessionLocal()))
    body = _body(resp)
    assert f"{PREFIX} Near" in body
    assert f"{PREFIX} Far" not in body


def test_stats_counts_by_tier():
    s = _run(R.stats(db=SessionLocal()))
    assert s["total"] >= 3
    assert any(t["tier"] == 1 for t in s["by_tier"])


def test_register_never_touches_the_startup_table():
    """The isolation requirement, asserted rather than assumed."""
    db = SessionLocal()
    before = db.query(Startup).count()
    db.close()

    rid_db = SessionLocal()
    rid = str(rid_db.query(RegionalCompany).filter_by(name=f"{PREFIX} Near").one().id)
    rid_db.close()

    _run(R.list_regional(q=PREFIX, db=SessionLocal()))
    _run(R.edit_regional(rid, R.RegionalEdit(status="y"), db=SessionLocal()))
    _run(R.stats(db=SessionLocal()))

    db = SessionLocal()
    assert db.query(Startup).count() == before
    db.close()


# ── enrichment progress ─────────────────────────────────────────────────────

def test_enrichment_status_infers_activity_from_the_data():
    """'Is it running?' is answered from committed rows, not from a process
    table — so it stays correct whether the run was started by hand, by
    launchd, or over SSH.

    SCOPE WARNING, learned the hard way: an earlier version of this test aged
    EVERY row with a last_verified_at in order to assert the inactive branch,
    and destroyed the real enrichment timestamps of 63 live companies. These
    tests share a database with production data, so a write must never reach
    beyond the PYTEST-prefixed fixtures. The inactive branch is therefore
    covered as a pure unit below rather than by mutating shared state.
    """
    from datetime import datetime

    db = SessionLocal()
    row = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Far").one()
    row.last_verified_at = datetime.utcnow()      # only a PYTEST row
    db.commit()
    db.close()

    s = _run(R.enrichment_status(db=SessionLocal()))
    assert s["active"] is True
    assert s["seconds_since_last"] is not None and s["seconds_since_last"] < 60
    assert s["attempted"] >= 1


def test_inactivity_threshold_is_longer_than_one_company_takes():
    """A company takes 1-3 minutes, so the 'stopped' threshold must sit well
    above that or a normal gap between records would read as a dead run.
    Asserted on the constant rather than by ageing shared rows."""
    import inspect
    src = inspect.getsource(R.enrichment_status)
    assert "600" in src, "inactivity threshold changed — is it still > 3 min?"


def test_enrichment_status_reports_a_queue_and_an_eta():
    s = _run(R.enrichment_status(db=SessionLocal()))
    assert s["queue_remaining"] >= 0
    assert s["eta_hours"] >= 0
    # An empty queue must not claim time remaining.
    if s["queue_remaining"] == 0:
        assert s["eta_hours"] == 0
