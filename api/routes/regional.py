"""
Regional company register API (Phase RC-5).

  GET   /regional            filtered, sorted, paginated list
  GET   /regional/facets     distinct Branchen / Status / cities, for filter menus
  GET   /regional/stats      counts by tier, source, radius — the KPI strip
  GET   /regional/{id}       one company, with per-field citations
  PATCH /regional/{id}       edit the CRM columns (human-only fields)
  GET   /regional/export.csv the whole filtered set as CSV

Deliberately a SEPARATE router from /scout, backing a separate dashboard page,
because the register must not be buried among 1,800 startups — that was the
owner's explicit requirement, not a styling preference. Nothing here reads or
writes the `startups` table.

WRITE SURFACE: only the five CRM columns are editable through the API. The
machine-gathered fields (employees, branche, kurzbeschreibung, website) are
owned by the discovery/enrichment pipeline and carry citations; letting the
dashboard overwrite them by hand would silently break the "every value has a
source" property the register is built on. A human who disagrees with a value
edits it in the source sheet or re-runs enrichment.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import RegionalCompany

router = APIRouter()
logger = logging.getLogger(__name__)

# Column order mirrors the team's Excel sheet so the page reads as the familiar
# artefact rather than a new tool. `entfernung` is the one addition — it is the
# whole point of a radius-defined register and the sheet never had it.
EXPORT_COLUMNS = [
    ("name", "Name"),
    ("city", "Standort"),
    ("distance_km", "Entfernung (km)"),
    ("employees", "Mitarbeiter"),
    ("branche", "Branche"),
    ("kurzbeschreibung", "Kurzbeschreibung"),
    ("website", "Website"),
    ("prio", "Prio"),
    ("kontakt", "Kontakt allgemein"),
    ("status", "Status"),
    ("phase", "Phase"),
    ("wer_hat_kontakt", "Wer hat Kontakt"),
    ("source", "Quelle"),
    ("source_url", "Quelle URL"),
]

# The only fields a dashboard edit may touch — see the module docstring.
EDITABLE_FIELDS = {"prio", "kontakt", "status", "phase", "wer_hat_kontakt", "notes"}


def _row(c: RegionalCompany) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "city": c.city,
        "distance_km": c.distance_km,
        "in_radius": c.in_radius,
        "employees": c.employees,
        "branche": c.branche,
        "kurzbeschreibung": c.kurzbeschreibung,
        "website": c.website,
        "prio": c.prio,
        "kontakt": c.kontakt,
        "status": c.status,
        "phase": c.phase,
        "wer_hat_kontakt": c.wer_hat_kontakt,
        "triage_tier": c.triage_tier,
        "source": c.source,
        "source_url": c.source_url,
        # Per-field citations, so the UI can show where each machine-gathered
        # value came from rather than asking anyone to take it on trust.
        "field_sources": c.field_sources or {},
        "notes": c.notes,
        "last_verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None,
    }


def _filtered(db: Session, *, q=None, tier=None, status=None, branche=None,
              city=None, min_employees=None, max_employees=None,
              max_distance=None, in_radius=True, missing_employees=False):
    query = db.query(RegionalCompany)

    if in_radius is not None:
        query = query.filter(RegionalCompany.in_radius.is_(bool(in_radius)))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(RegionalCompany.name.ilike(like),
                                 RegionalCompany.branche.ilike(like),
                                 RegionalCompany.kurzbeschreibung.ilike(like),
                                 RegionalCompany.city.ilike(like)))
    if tier is not None:
        query = query.filter(RegionalCompany.triage_tier == tier)
    if status:
        query = (query.filter(RegionalCompany.status.is_(None))
                 if status == "(leer)" else
                 query.filter(RegionalCompany.status == status))
    if branche:
        query = query.filter(RegionalCompany.branche == branche)
    if city:
        query = query.filter(RegionalCompany.city == city)
    if min_employees is not None:
        query = query.filter(RegionalCompany.employees >= min_employees)
    if max_employees is not None:
        query = query.filter(RegionalCompany.employees <= max_employees)
    if max_distance is not None:
        query = query.filter(RegionalCompany.distance_km <= max_distance)
    if missing_employees:
        query = query.filter(RegionalCompany.employees.is_(None))
    return query


@router.get("")
async def list_regional(
    q: Optional[str] = None,
    tier: Optional[int] = None,
    status: Optional[str] = None,
    branche: Optional[str] = None,
    city: Optional[str] = None,
    min_employees: Optional[int] = None,
    max_employees: Optional[int] = None,
    max_distance: Optional[float] = None,
    in_radius: bool = True,
    missing_employees: bool = False,
    sort: str = "distance",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    # Plain int defaults rather than Query(...) so the endpoint stays callable
    # directly, which is how this project's route tests exercise it.
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    query = _filtered(db, q=q, tier=tier, status=status, branche=branche, city=city,
                      min_employees=min_employees, max_employees=max_employees,
                      max_distance=max_distance, in_radius=in_radius,
                      missing_employees=missing_employees)
    total = query.count()

    # Nearest-first by default: for partnership outreach a company 3 km away
    # matters more than one 48 km away, so proximity is the natural order.
    if sort == "employees":
        query = query.order_by(RegionalCompany.employees.desc().nullslast())
    elif sort == "name":
        query = query.order_by(RegionalCompany.name)
    elif sort == "tier":
        query = query.order_by(RegionalCompany.triage_tier.asc().nullslast(),
                               RegionalCompany.distance_km.asc().nullslast())
    else:
        query = query.order_by(RegionalCompany.distance_km.asc().nullslast())

    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset,
            "companies": [_row(c) for c in rows]}


@router.get("/facets")
async def facets(db: Session = Depends(get_db)):
    """Distinct values actually present, so filter menus never offer a choice
    that returns nothing."""
    def distinct(col, limit=200):
        return [v for (v,) in db.query(col).filter(col.isnot(None))
                .distinct().order_by(col).limit(limit).all() if str(v).strip()]
    return {
        "branchen": distinct(RegionalCompany.branche),
        "cities": distinct(RegionalCompany.city, 400),
        "statuses": distinct(RegionalCompany.status),
    }


@router.get("/stats")
async def stats(db: Session = Depends(get_db)):
    from regional.triage import LABELS
    in_radius = db.query(RegionalCompany).filter(RegionalCompany.in_radius.is_(True))
    by_tier = dict(db.query(RegionalCompany.triage_tier, func.count())
                   .filter(RegionalCompany.in_radius.is_(True))
                   .group_by(RegionalCompany.triage_tier).all())
    by_source = dict(db.query(RegionalCompany.source, func.count())
                     .group_by(RegionalCompany.source).all())
    return {
        "total": db.query(RegionalCompany).count(),
        "in_radius": in_radius.count(),
        "out_of_radius": db.query(RegionalCompany)
                           .filter(RegionalCompany.in_radius.is_(False)).count(),
        "with_employees": in_radius.filter(
            RegionalCompany.employees.isnot(None)).count(),
        "contacted": in_radius.filter(RegionalCompany.status.isnot(None)).count(),
        "by_tier": [{"tier": t, "label": LABELS.get(t, "?"), "count": n}
                    for t, n in sorted(by_tier.items(), key=lambda kv: (kv[0] is None, kv[0]))],
        "by_source": by_source,
    }


@router.get("/enrichment-status")
async def enrichment_status(db: Session = Depends(get_db)):
    """
    Progress of the background enrichment run, for the dashboard.

    "Is it running?" is answered from the DATA rather than by inspecting
    processes: the runner commits after every company, so a recent
    last_verified_at is direct evidence of work happening. That keeps this
    endpoint honest if the job is started by hand, by launchd, or over SSH —
    it reports what the register actually received, not what some process
    table claims.

    NOTE: this route must stay declared BEFORE /{company_id}, or the path
    parameter swallows it and "enrichment-status" is treated as an id.
    """
    in_radius = db.query(RegionalCompany).filter(RegionalCompany.in_radius.is_(True))

    queue = in_radius.filter(RegionalCompany.triage_tier == 2,
                             RegionalCompany.employees.is_(None),
                             RegionalCompany.last_verified_at.is_(None)).count()
    attempted = in_radius.filter(RegionalCompany.last_verified_at.isnot(None)).count()
    with_employees = in_radius.filter(RegionalCompany.employees.isnot(None)).count()

    last = (db.query(func.max(RegionalCompany.last_verified_at))
              .filter(RegionalCompany.in_radius.is_(True)).scalar())

    active, secs_since = False, None
    if last:
        secs_since = (datetime.utcnow() - last).total_seconds()
        # A company takes 1-3 minutes, so silence beyond 10 means the run has
        # stopped or is wedged — not merely between records.
        active = secs_since < 600

    # Measured rather than assumed: ~2.3 min/company on this machine.
    eta_hours = round(queue * 2.3 / 60, 1) if queue else 0.0

    return {
        "active": active,
        "queue_remaining": queue,
        "attempted": attempted,
        "with_employees": with_employees,
        "last_activity": last.isoformat() if last else None,
        "seconds_since_last": int(secs_since) if secs_since is not None else None,
        "eta_hours": eta_hours,
    }


@router.get("/export.csv")
async def export_csv(
    q: Optional[str] = None,
    tier: Optional[int] = None,
    status: Optional[str] = None,
    branche: Optional[str] = None,
    city: Optional[str] = None,
    min_employees: Optional[int] = None,
    max_employees: Optional[int] = None,
    max_distance: Optional[float] = None,
    in_radius: bool = True,
    missing_employees: bool = False,
    db: Session = Depends(get_db),
):
    """
    Export the CURRENT filtered set, not just the loaded page — the team works
    in Excel and shares that file with people who will never open the
    dashboard, so a pipeline that cannot hand back a spreadsheet would be a
    downgrade from what they already have.

    UTF-8 BOM and ';' delimiter: German Excel defaults to semicolons and
    misreads plain UTF-8, which would mangle every umlaut in the file.
    """
    rows = _filtered(db, q=q, tier=tier, status=status, branche=branche, city=city,
                     min_employees=min_employees, max_employees=max_employees,
                     max_distance=max_distance, in_radius=in_radius,
                     missing_employees=missing_employees) \
        .order_by(RegionalCompany.distance_km.asc().nullslast()).all()

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([label for _, label in EXPORT_COLUMNS])
    for c in rows:
        writer.writerow([getattr(c, attr, None) if getattr(c, attr, None) is not None
                         else "" for attr, _ in EXPORT_COLUMNS])

    payload = "﻿" + buf.getvalue()
    fname = f"regionale-unternehmen-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/{company_id}")
async def get_regional(company_id: str, db: Session = Depends(get_db)):
    c = db.query(RegionalCompany).filter(RegionalCompany.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    return _row(c)


class RegionalEdit(BaseModel):
    prio: Optional[int] = None
    kontakt: Optional[str] = None
    status: Optional[str] = None
    phase: Optional[str] = None
    wer_hat_kontakt: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/{company_id}")
async def edit_regional(company_id: str, edit: RegionalEdit,
                        db: Session = Depends(get_db)):
    """
    Edit the CRM columns. Only the human-owned fields are writable — an attempt
    to change `employees` or `branche` through here is ignored rather than
    honoured, because those carry citations that a hand edit would invalidate.
    """
    c = db.query(RegionalCompany).filter(RegionalCompany.id == company_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")

    applied = []
    for field, value in edit.dict(exclude_unset=True).items():
        if field not in EDITABLE_FIELDS:
            continue
        setattr(c, field, value)
        applied.append(field)

    if applied:
        db.commit()
        logger.info(f"[Regional] edited {c.name}: {', '.join(applied)}")
    return {"status": "ok", "applied": applied, "company": _row(c)}
