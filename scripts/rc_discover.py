"""
Discover regional companies from free sources (Phase RC-2).

Queries Wikidata, German Wikipedia regional categories and OpenStreetMap,
filters institutional junk and out-of-radius hits, dedupes across sources and
against the existing register, and reports what is genuinely NEW.

Dry-run by default. Nothing is written until --apply.

Usage:
    python3 scripts/rc_discover.py                      # preview everything
    python3 scripts/rc_discover.py --sources wikidata   # one source
    python3 scripts/rc_discover.py --min-employees 100  # only sized hits
    python3 scripts/rc_discover.py --apply              # write new candidates
"""
import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal          # noqa: E402
from database.models import RegionalCompany           # noqa: E402
from processing.deduplicator import normalize_company_name  # noqa: E402
from regional import discovery, filters, matcher, triage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="Free-source discovery (RC-2)")
    ap.add_argument("--sources", nargs="*", default=None,
                    choices=list(discovery.SOURCES),
                    help="default: all three")
    ap.add_argument("--radius", type=int, default=50, help="km from Memmingen")
    ap.add_argument("--min-employees", type=int, default=filters.DEFAULT_MIN_EMPLOYEES,
                    help=f"band floor (default {filters.DEFAULT_MIN_EMPLOYEES})")
    ap.add_argument("--max-employees", type=int, default=filters.DEFAULT_MAX_EMPLOYEES,
                    help=f"band ceiling (default {filters.DEFAULT_MAX_EMPLOYEES})")
    ap.add_argument("--apply", action="store_true", help="write new candidates")
    args = ap.parse_args()

    print(f"Discovering companies within {args.radius} km of Memmingen "
          f"(sources: {', '.join(args.sources or discovery.SOURCES)})\n")

    raw = discovery.discover(sources=args.sources, radius_km=args.radius)
    print(f"\nraw candidates from all sources: {len(raw)}")

    buckets = discovery.filter_candidates(raw, radius_km=args.radius)
    print(f"  dropped as institutions/junk : {len(buckets['junk'])}")
    print(f"  outside {args.radius} km             : {len(buckets['out_of_radius'])}")
    print(f"  no coordinates (needs geocode): {len(buckets['needs_geocoding'])}")
    print(f"  inside radius                : {len(buckets['kept'])}")

    # Wikipedia candidates carry no coordinates; they'd be lost if dropped, so
    # they ride along and get geocoded later (RC-4) rather than discarded.
    pool = buckets["kept"] + buckets["needs_geocoding"]
    deduped, collapsed = matcher.dedupe(pool)
    print(f"\nafter cross-source dedupe: {len(deduped)}  ({collapsed} collapsed)")

    db = SessionLocal()
    try:
        existing = [r.name for r in db.query(RegionalCompany.name).all()]
        print(f"already in the register  : {len(existing)}")

        new, known = matcher.split_new_vs_known(deduped, existing)
        print(f"\n  already known : {len(known)}")
        print(f"  NEW           : {len(new)}")

        in_band = [c for c in new
                   if filters.in_employee_band(c.employees,
                                               lo=args.min_employees,
                                               hi=args.max_employees)]
        too_big = [c for c in new
                   if c.employees is not None and c.employees > args.max_employees]

        print(f"\n{'='*76}")
        print(f"NEW in the {args.min_employees}-{args.max_employees} employee band "
              f"({len(in_band)})")
        print(f"{'='*76}")
        for c in sorted(in_band, key=lambda x: -(x.employees or 0)):
            d = f"{c.distance_km:.0f} km" if c.distance_km is not None else "  ?  "
            print(f"  {str(c.employees):>6}  {d:>7}  {c.name[:38]:<38} "
                  f"{(c.city or '')[:16]:<16} {c.source}")

        if too_big:
            print(f"\n  Above the band, excluded ({len(too_big)}): "
                  + ", ".join(f"{c.name} ({c.employees})"
                              for c in sorted(too_big, key=lambda x: -x.employees)[:8]))

        unsized = [c for c in new if c.employees is None]
        print(f"\n{'='*76}")
        print(f"NEW without a headcount ({len(unsized)}) — RC-4 enrichment queue")
        print(f"{'='*76}")
        for c in sorted(unsized, key=lambda x: (x.distance_km is None,
                                                x.distance_km or 0))[:60]:
            d = f"{c.distance_km:.0f} km" if c.distance_km is not None else "  ?  "
            print(f"          {d:>7}  {c.name[:38]:<38} "
                  f"{(c.city or '')[:16]:<16} {c.source}")
        if len(unsized) > 60:
            print(f"          ... and {len(unsized) - 60} more")

        if args.apply:
            written = 0
            for c in new:
                db.add(RegionalCompany(
                    name=c.name,
                    normalized_name=normalize_company_name(c.name),
                    city=c.city,
                    employees=c.employees,
                    branche=c.branche,
                    website=c.website,
                    lat=c.lat, lon=c.lon,
                    distance_km=c.distance_km,
                    in_radius=(c.distance_km is not None
                               and c.distance_km <= args.radius),
                    source=c.source,
                    source_url=c.source_url,
                    triage_tier=triage.tier_for(employees=c.employees,
                                                source=c.source),
                    raw={**(c.raw or {}),
                         "discovered_at": datetime.utcnow().isoformat()},
                ))
                written += 1
            db.commit()
            print(f"\n  ✓ wrote {written} new companies to the register")
        else:
            print(f"\n  DRY RUN — nothing written. Re-run with --apply to add "
                  f"the {len(new)} new companies.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
