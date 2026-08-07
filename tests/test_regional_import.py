"""
Phase RC-1 — regional register import.

Covers the parsing edge cases the real sheet actually contains (employee
counts written as "1.200" and "??", missing Standort) and the two behaviours
that protect real work: a re-import must UPDATE rather than duplicate, and it
must never blank a human's CRM edit just because the re-exported sheet had an
empty cell.

Geocoding is monkeypatched throughout — these tests must not hit Nominatim.
"""
import os
import tempfile

import pytest

from database.connection import SessionLocal
from database.models import RegionalCompany
from regional import importer

PREFIX = "PYTEST-RC"

_FAKE_PLACES = {
    "Memmingen": {"lat": 47.9878, "lon": 10.1815, "distance_km": 0.0,
                  "in_radius": True, "postcode": "87700", "display_name": "Memmingen"},
    "Kempten": {"lat": 47.7267, "lon": 10.3168, "distance_km": 30.7,
                "in_radius": True, "postcode": "87435", "display_name": "Kempten"},
    "Augsburg": {"lat": 48.3705, "lon": 10.8978, "distance_km": 68.0,
                 "in_radius": False, "postcode": "86150", "display_name": "Augsburg"},
    "Sengenthal": {"lat": 49.2500, "lon": 11.4700, "distance_km": 167.3,
                   "in_radius": False, "postcode": None, "display_name": "Sengenthal"},
}
_UNRESOLVED = {"lat": None, "lon": None, "distance_km": None,
               "in_radius": False, "postcode": None, "display_name": None}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(importer, "locate",
                        lambda city, **kw: _FAKE_PLACES.get(city, dict(_UNRESOLVED)))


@pytest.fixture(autouse=True)
def _clean():
    def purge():
        db = SessionLocal()
        try:
            db.query(RegionalCompany).filter(
                RegionalCompany.name.like(f"{PREFIX}%")).delete(
                    synchronize_session=False)
            db.commit()
        finally:
            db.close()
    purge()
    yield
    purge()


def _csv(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8")
    fh.write(text)
    fh.close()
    return fh.name


def test_parses_the_real_sheets_awkward_values():
    path = _csv(
        "Name,Standort,Mitarbeiter,Branche,Kurzbeschreibung\n"
        f"{PREFIX} Dotted,Memmingen,1.200,Verpackung,Wellpappe\n"
        f"{PREFIX} Unknown,Memmingen,??,Schaumstoff,Schaum\n"
        f"{PREFIX} Blank,Memmingen,,Verlag,Zeitung\n"
    )
    try:
        rows = importer.parse_csv(path)
    finally:
        os.unlink(path)

    by_name = {r["name"]: r for r in rows}
    assert by_name[f"{PREFIX} Dotted"]["employees"] == 1200  # "1.200" -> 1200
    assert by_name[f"{PREFIX} Unknown"]["employees"] is None  # "??" -> None
    assert by_name[f"{PREFIX} Blank"]["employees"] is None


def test_umlaut_and_spacing_insensitive_headers():
    """The sheet's headers are German and inconsistently spaced; matching must
    not depend on exact bytes."""
    path = _csv(
        "NAME;Standort;MITARBEITER;Branche;Kurzbeschreibung;Prio;Wer hat Kontakt\n"
        f"{PREFIX} Header,Memmingen,400,Hydraulik,Ventile,1,FF\n".replace(",", ";")
    )
    try:
        rows = importer.parse_csv(path)
    finally:
        os.unlink(path)
    assert rows[0]["employees"] == 400
    assert rows[0]["wer_hat_kontakt"] == "FF"
    assert rows[0]["prio"] == 1


def test_radius_flag_and_distance():
    path = _csv(
        "Name,Standort,Mitarbeiter\n"
        f"{PREFIX} Near,Memmingen,500\n"
        f"{PREFIX} Far,Augsburg,1701\n"
    )
    db = SessionLocal()
    try:
        stats = importer.import_rows(db, importer.parse_csv(path), apply=True)
        assert stats["in_radius"] == 1
        assert stats["out_of_radius"] == 1
        near = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Near").one()
        far = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Far").one()
        assert near.in_radius is True and near.distance_km == 0.0
        # Out-of-radius rows are KEPT, not dropped — they carry contact history.
        assert far.in_radius is False and far.distance_km == 68.0
    finally:
        os.unlink(path)
        db.close()


def test_unresolvable_standort_is_named_not_just_counted():
    """The real sheet says 'Segenthal' for Max Bögl, which is not a place.
    A silent skip would leave the error in the sheet forever."""
    path = _csv(f"Name,Standort\n{PREFIX} Bogl,Segenthal\n")
    db = SessionLocal()
    try:
        stats = importer.import_rows(db, importer.parse_csv(path), apply=True)
        assert stats["geocode_failed"] == 1
        assert any(f"{PREFIX} Bogl" in u for u in stats["unresolved"])
        row = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Bogl").one()
        assert row.in_radius is False
        assert row.notes and "could not be geocoded" in row.notes
    finally:
        os.unlink(path)
        db.close()


def test_reimport_updates_and_never_blanks_a_human_crm_edit():
    """The behaviour that protects real work: re-importing a refreshed sheet
    must not duplicate rows, and must not wipe a Status someone typed into the
    dashboard just because the exported CSV has that cell empty."""
    first = _csv(
        "Name,Standort,Mitarbeiter,Branche,Status\n"
        f"{PREFIX} Keep,Kempten,400,Hydraulik,\n"
    )
    db = SessionLocal()
    try:
        importer.import_rows(db, importer.parse_csv(first), apply=True)
        row = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Keep").one()

        # A human then records an outcome in the dashboard.
        row.status = "Absage"
        row.wer_hat_kontakt = "AH"
        db.commit()

        # The sheet is re-exported later with a refreshed headcount and,
        # as usual, an empty Status column.
        second = _csv(
            "Name,Standort,Mitarbeiter,Branche,Status\n"
            f"{PREFIX} Keep,Kempten,450,Hydraulik,\n"
        )
        try:
            stats = importer.import_rows(db, importer.parse_csv(second), apply=True)
        finally:
            os.unlink(second)

        assert stats["inserted"] == 0 and stats["updated"] == 1
        assert db.query(RegionalCompany).filter_by(name=f"{PREFIX} Keep").count() == 1

        db.expire_all()
        row = db.query(RegionalCompany).filter_by(name=f"{PREFIX} Keep").one()
        assert row.employees == 450        # machine field refreshed
        assert row.status == "Absage"      # human field preserved
        assert row.wer_hat_kontakt == "AH"
    finally:
        os.unlink(first)
        db.close()


def test_dry_run_writes_nothing():
    path = _csv(f"Name,Standort,Mitarbeiter\n{PREFIX} Ghost,Memmingen,100\n")
    db = SessionLocal()
    try:
        stats = importer.import_rows(db, importer.parse_csv(path), apply=False)
        assert stats["inserted"] == 1  # reported...
        db.expire_all()
        assert db.query(RegionalCompany).filter_by(name=f"{PREFIX} Ghost").count() == 0
    finally:
        os.unlink(path)
        db.close()
