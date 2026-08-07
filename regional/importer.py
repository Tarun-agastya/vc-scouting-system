"""
Import the hand-maintained "Potenzielle Mitglieder" sheet (Phase RC-1).

Reads a CSV exported from the team's Excel sheet rather than carrying the data
in-repo. Two reasons, and the second is the important one:

  1. The sheet keeps changing. An importable file means a re-import whenever
     they update it, instead of a code change.
  2. Its CRM half holds named individuals' direct phone numbers and e-mail
     addresses (Geschäftsführer contacts). Committing real people's contact
     details into a git repository is a GDPR exposure with no upside — the
     data belongs in the database, not in version control. `regional/data/`
     is gitignored for exactly this reason.

Upsert key is (normalized_name, city), reusing the project's own
`normalize_company_name` so a re-import updates rather than duplicates, and so
this register dedupes by the same rules as everything else.

CRM columns are written ONLY on first insert and only from the sheet — a
re-import never blanks a human's later edit with an empty cell.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from typing import Optional

from processing.deduplicator import normalize_company_name
from regional.geocode import locate

logger = logging.getLogger(__name__)

# Sheet header -> model attribute. Matching is case/space/umlaut-insensitive
# (see _norm_header) so "Wer hat Kontakt", "wer_hat_kontakt" and
# "WER HAT KONTAKT" all land correctly.
COLUMN_MAP = {
    # the five data columns
    "name": "name",
    "standort": "city",
    "mitarbeiter": "employees",
    "branche": "branche",
    "kurzbeschreibung": "kurzbeschreibung",
    # the five CRM columns — human-owned
    "prio": "prio",
    "kontaktallgemein": "kontakt",
    "kontakt": "kontakt",
    "status": "status",
    "phase": "phase",
    "werhatkontakt": "wer_hat_kontakt",
    # optional extras if present
    "website": "website",
}

CRM_FIELDS = {"prio", "kontakt", "status", "phase", "wer_hat_kontakt"}

# A Standort this far from Memmingen is almost certainly a wrong or ambiguous
# place name rather than a genuine entry — the real sheet contained
# "Segenthal" for Max Bögl, which geocodes to Sengenthal near Neumarkt, 167 km
# away. Flagged loudly rather than silently corrected: only a human knows
# whether the location or the company is the mistake.
_IMPLAUSIBLE_DISTANCE_KM = 100.0


def _norm_header(h: str) -> str:
    h = (h or "").strip().lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        h = h.replace(a, b)
    return re.sub(r"[^a-z]", "", h)


def _parse_employees(raw) -> Optional[int]:
    """'1200' | '1.200' | '1,200' | 'ca. 400' | '??' | '' -> int | None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in {"?", "??", "-", "n/a"}:
        return None
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    try:
        val = int(digits)
    except ValueError:
        return None
    return val if 0 < val < 10_000_000 else None


def _parse_prio(raw) -> Optional[int]:
    if raw is None:
        return None
    m = re.search(r"\d+", str(raw))
    return int(m.group()) if m else None


def _clean(raw) -> Optional[str]:
    if raw is None:
        return None
    s = " ".join(str(raw).split())
    return s or None


def parse_csv(path: str) -> list[dict]:
    """CSV rows -> normalised dicts. Rows with no usable Name are skipped."""
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)

        for raw_row in reader:
            rec: dict = {}
            for header, value in raw_row.items():
                attr = COLUMN_MAP.get(_norm_header(header or ""))
                if not attr:
                    continue
                if attr == "employees":
                    rec[attr] = _parse_employees(value)
                elif attr == "prio":
                    rec[attr] = _parse_prio(value)
                else:
                    rec[attr] = _clean(value)

            if not rec.get("name"):
                continue
            rows.append(rec)
    return rows


def import_rows(db, rows: list[dict], *, apply: bool = False,
                source: str = "sheet") -> dict:
    """
    Geocode, compute distance, and upsert. Dry-run by default (apply=False) —
    the same discipline as scripts/dedup_sweep.py: show what would change,
    commit only when explicitly told to.
    """
    from database.models import RegionalCompany

    stats = {"parsed": len(rows), "inserted": 0, "updated": 0,
             "skipped_no_city": 0, "geocode_failed": 0,
             "in_radius": 0, "out_of_radius": 0,
             "warnings": [], "unresolved": []}

    for rec in rows:
        name = rec["name"]
        city = rec.get("city")
        norm = normalize_company_name(name)

        geo = {"lat": None, "lon": None, "distance_km": None,
               "in_radius": False, "postcode": None, "display_name": None}
        note = None
        if city:
            geo = locate(city)
            if geo["lat"] is None:
                # An unresolvable Standort is a data-quality signal worth
                # surfacing by name, not just counting: the real sheet had
                # "Segenthal" for Max Bögl, which is not a place — the company
                # is in Sengenthal, 167 km away and far out of scope. Name it
                # so a human can fix the source sheet.
                stats["geocode_failed"] += 1
                stats["unresolved"].append(f"{name} — Standort {city!r} not found")
                note = (f"Standort {city!r} could not be geocoded — check the "
                        f"spelling in the source sheet.")
        else:
            stats["skipped_no_city"] += 1

        d = geo["distance_km"]
        if d is not None and d > _IMPLAUSIBLE_DISTANCE_KM:
            note = (f"Standort {city!r} geocodes {d:.0f} km from Memmingen — "
                    f"likely a wrong or ambiguous place name; please check.")
            stats["warnings"].append(f"{name}: {note}")

        if geo["in_radius"]:
            stats["in_radius"] += 1
        else:
            stats["out_of_radius"] += 1

        existing = (db.query(RegionalCompany)
                      .filter(RegionalCompany.normalized_name == norm,
                              RegionalCompany.city == city)
                      .first())

        if existing:
            # Refresh machine-owned fields only; never blank a human's CRM
            # edit because the re-exported sheet happened to have an empty cell.
            for attr in ("employees", "branche", "kurzbeschreibung", "website"):
                val = rec.get(attr)
                if val not in (None, ""):
                    setattr(existing, attr, val)
            for k, v in geo.items():
                if k != "display_name" and hasattr(existing, k):
                    setattr(existing, k, v)
            if note:
                existing.notes = note
            stats["updated"] += 1
        else:
            company = RegionalCompany(
                name=name,
                normalized_name=norm,
                city=city,
                employees=rec.get("employees"),
                branche=rec.get("branche"),
                kurzbeschreibung=rec.get("kurzbeschreibung"),
                website=rec.get("website"),
                lat=geo["lat"], lon=geo["lon"],
                distance_km=geo["distance_km"], in_radius=geo["in_radius"],
                postcode=geo["postcode"],
                # CRM columns seeded from the sheet on first insert only
                prio=rec.get("prio"),
                kontakt=rec.get("kontakt"),
                status=rec.get("status"),
                phase=rec.get("phase"),
                wer_hat_kontakt=rec.get("wer_hat_kontakt"),
                source=source,
                raw={"imported_at": datetime.utcnow().isoformat(),
                     "geocoded_as": geo.get("display_name")},
                notes=note,
            )
            db.add(company)
            stats["inserted"] += 1

    if apply:
        db.commit()
    else:
        db.rollback()

    return stats
