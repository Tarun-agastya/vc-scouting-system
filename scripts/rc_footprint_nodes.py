"""
Extend the OSM footprint proxy to point-marker companies (Phase RC-3c).

scripts/rc_footprints.py measures every company mapped as a building POLYGON.
That leaves companies mapped as a bare POINT with no shape to measure —
this script closes part of that gap by looking for the company's own building
nearby, but ONLY when the match is unambiguous. See
regional/footprint_proxy.py's module docstring for the exact safety rule.

Anything that isn't a safe match is left alone — footprint_m2 stays NULL and
the company correctly stays in the low-prior tier. This script never promotes
a company on a guess.

Dry run by default.

Usage:
    python3 scripts/rc_footprint_nodes.py
    python3 scripts/rc_footprint_nodes.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal        # noqa: E402
from database.models import RegionalCompany         # noqa: E402
from regional import footprint_proxy, triage         # noqa: E402

BATCH = 25   # smaller than the way-id batches: an `around` union query is
             # heavier per-point than a plain id lookup, and Overpass has
             # been returning 504s on roughly a third of larger calls.


def main() -> int:
    ap = argparse.ArgumentParser(description="Node->building footprint proxy (RC-3c)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--radius", type=float, default=footprint_proxy.SEARCH_RADIUS_M)
    ap.add_argument("--threshold", type=float, default=triage.LARGE_FOOTPRINT_M2)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        rows = [c for c in db.query(RegionalCompany)
                .filter(RegionalCompany.in_radius.is_(True),
                       RegionalCompany.source == "osm",
                       RegionalCompany.footprint_m2.is_(None),
                       RegionalCompany.lat.isnot(None),
                       RegionalCompany.lon.isnot(None)).all()
                if c.source_url and "/node/" in (c.source_url or "")]
        print(f"unmeasured OSM node companies: {len(rows)}")
        if not rows:
            print("  nothing to match")
            return 0

        matched = 0
        by_method = {"name_match": 0, "unambiguous_nearest": 0}
        for i in range(0, len(rows), args.batch):
            batch = rows[i:i + args.batch]
            results = footprint_proxy.match_batch(batch, radius_m=args.radius)
            for c in batch:
                m = results.get(str(c.id))
                if not m:
                    continue
                matched += 1
                by_method[m.method] += 1
                c.footprint_m2 = m.area_m2
                c.raw = {**(c.raw or {}), "footprint_proxy": {
                    "method": m.method, "distance_m": m.distance_m,
                    "matched_building_osm_id": m.osm_id,
                    "matched_name": m.matched_name,
                }}
                print(f"  [{m.method:<20}] {m.area_m2:>8,.0f} m²  "
                      f"({m.distance_m:>4.0f}m away)  {c.name[:40]}")
            print(f"  processed {min(i + args.batch, len(rows))}/{len(rows)}"
                  f"  — matched so far: {matched}")

        unresolved = len(rows) - matched
        print(f"\n  matched (name match)        {by_method['name_match']}")
        print(f"  matched (unambiguous nearest) {by_method['unambiguous_nearest']}")
        print(f"  left unresolved -> stays tier 3  {unresolved}")

        newly_sized = [c for c in rows if c.footprint_m2 is not None]
        promotable = [c for c in newly_sized
                     if (c.footprint_m2 or 0) >= args.threshold and c.employees is None]
        print(f"  of which promotable (>= {args.threshold:,.0f} m²) {len(promotable)}")

        if args.apply:
            for c in promotable:
                # Pass revenue through too, even though this script never sets it
                # itself — a footprint-triggered recompute must not silently
                # regress a company an earlier enrichment run already
                # qualified via revenue.
                c.triage_tier = triage.tier_for(employees=c.employees, source=c.source,
                                                footprint_m2=c.footprint_m2,
                                                revenue_eur_millions=c.revenue_eur_millions,
                                                revenue_year=c.revenue_year)
            db.commit()
            print(f"\n  ✓ stored {matched} proxy footprints, "
                  f"promoted {len(promotable)} into the enrichment queue")
        else:
            db.rollback()
            print("\n  DRY RUN — nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
