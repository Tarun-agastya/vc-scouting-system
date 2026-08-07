"""
De-duplicate the regional register with location-aware matching (RC-2b).

The register was built with a name-only matcher, which left confirmed
duplicate rows behind — measured 7 Aug: 3M Ceramics / 3M Technical Ceramics,
Autohaus Seitz / Seitz-Gruppe, Kolb Wellpappe / Hans Kolb Wellpappe, Urban
GmbH / Urban Maschinenbau. Each pair is one company under two renderings, and
each was split because name similarity alone (43-85) sat below the bar that
name-only matching needs in order not to merge genuinely different firms
sharing a surname. Location resolves it: those four pairs share a town, and
the Schmid pair that must stay apart does not.

MERGE POLICY — the sheet always wins as the survivor when one is involved,
regardless of how much machine-gathered data the other row carries. A sheet
row holds human CRM state (Prio, Status, Phase, who spoke to whom) that
exists nowhere else and cannot be re-derived; a discovered row holds facts
that can always be re-fetched. Losing the former to keep the latter would be
a bad trade even though the discovered row often looks "richer".

Empty fields on the survivor are filled from the loser, and the loser's
provenance is preserved in the survivor's `raw` so nothing is silently lost.

Dry run by default.

Usage:
    python3 scripts/rc_dedupe.py
    python3 scripts/rc_dedupe.py --apply
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal      # noqa: E402
from database.models import RegionalCompany       # noqa: E402
from regional import matcher                      # noqa: E402

# Fields safe to copy from a losing row into an empty slot on the survivor.
# The CRM columns are absent on purpose — they are human-owned, and a
# discovered row never has them anyway.
_FILLABLE = ("city", "employees", "branche", "kurzbeschreibung", "website",
             "lat", "lon", "distance_km", "postcode", "source_url")


def _coords(r):
    return (r.lat, r.lon) if r.lat is not None else None


def _survivor(a, b):
    """Pick which row lives. Sheet rows always win — see the module docstring."""
    if (a.source == "sheet") != (b.source == "sheet"):
        return (a, b) if a.source == "sheet" else (b, a)
    # Otherwise prefer the one carrying more evidence, then the older row.
    def rank(r):
        return (r.employees is not None, r.lat is not None,
                bool(r.website), bool(r.kurzbeschreibung), bool(r.branche))
    return (a, b) if rank(a) >= rank(b) else (b, a)


def main() -> int:
    ap = argparse.ArgumentParser(description="De-duplicate the register (RC-2b)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(RegionalCompany).order_by(RegionalCompany.created_at).all()
        print(f"scanning {len(rows)} rows with location-aware matching...\n")

        kept, pairs = [], []
        for row in rows:
            hit = None
            for k in kept:
                if matcher.is_same(row.name, k.name,
                                   a_city=row.city, b_city=k.city,
                                   a_coords=_coords(row), b_coords=_coords(k)):
                    hit = k
                    break
            if hit is None:
                kept.append(row)
            else:
                pairs.append((hit, row))

        print(f"found {len(pairs)} duplicate pair(s)\n")
        merged = 0
        for a, b in pairs:
            keep, drop = _survivor(a, b)
            filled = []
            for attr in _FILLABLE:
                if getattr(keep, attr, None) in (None, "", 0):
                    val = getattr(drop, attr, None)
                    if val not in (None, "", 0):
                        if args.apply:
                            setattr(keep, attr, val)
                        filled.append(attr)

            print(f"  keep  {keep.name[:40]:<40} ({keep.source})")
            print(f"  drop  {drop.name[:40]:<40} ({drop.source})"
                  + (f"   -> fills {', '.join(filled)}" if filled else ""))

            if args.apply:
                keep.raw = {**(keep.raw or {}), "merged_from": [
                    *(keep.raw or {}).get("merged_from", []),
                    {"name": drop.name, "source": drop.source,
                     "source_url": drop.source_url,
                     "merged_at": datetime.utcnow().isoformat()},
                ]}
                db.delete(drop)
                merged += 1

        if args.apply:
            db.commit()
            print(f"\n  ✓ merged {merged} duplicate(s); "
                  f"{db.query(RegionalCompany).count()} rows remain")
        else:
            print(f"\n  DRY RUN — nothing changed. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
