"""
Google Places discovery sweep for the regional register (RC-3, Lever D).

THIS IS THE ONLY SCRIPT IN THE PROJECT THAT SPENDS MONEY. It therefore
defaults to --estimate, which computes the tile count and projected cost
without calling the API at all. Nothing is charged and nothing is written
until you pass --run, and --run still stops dead at the call cap.

    python3 scripts/rc_google_places.py                     # cost estimate only
    python3 scripts/rc_google_places.py --run --max-calls 40  # small real probe
    python3 scripts/rc_google_places.py --run --apply         # sweep + write

Needs GOOGLE_PLACES_API_KEY in .env (Google Cloud -> Places API (New)).

Recommended first step is a small probe (--max-calls 40, ~$1.30) over the town
centre. That measures what Google actually adds over the free sources on THIS
region before committing to a full sweep — the same measure-then-decide
discipline used for the free sources.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings                        # noqa: E402
from database.connection import SessionLocal       # noqa: E402
from database.models import RegionalCompany        # noqa: E402
from processing.deduplicator import normalize_company_name  # noqa: E402
from regional import filters, google_places, matcher, triage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="Google Places sweep (RC-3)")
    ap.add_argument("--run", action="store_true",
                    help="actually call the API (costs money)")
    ap.add_argument("--apply", action="store_true",
                    help="write new companies (implies --run)")
    ap.add_argument("--radius", type=float, default=50.0)
    ap.add_argument("--tile-km", type=float, default=settings.google_places_tile_km)
    ap.add_argument("--max-calls", type=int, default=settings.google_places_max_calls)
    args = ap.parse_args()

    est = google_places.estimate_sweep(radius_km=args.radius, tile_km=args.tile_km)
    print(f"Sweep plan: {args.radius:.0f} km radius, {args.tile_km} km tiles")
    print(f"  tiles                {est['tiles']}")
    print(f"  minimum calls        {est['min_calls']}")
    print(f"  estimated cost       ~${est['estimated_usd']}  ({est['note']})")
    print(f"  your call cap        {args.max_calls}  "
          f"(~${round(args.max_calls / 1000 * google_places.USD_PER_1000, 2)} max)")
    print(f"  types                {', '.join(google_places.B2B_TYPES)}")

    if not (args.run or args.apply):
        print("\n  ESTIMATE ONLY — no API calls made, nothing charged.")
        print("  Add --run for a real sweep (still bounded by --max-calls).")
        return 0

    key = settings.google_places_api_key
    if not key:
        print("\n  GOOGLE_PLACES_API_KEY is not set. Add it to .env:")
        print("    GOOGLE_PLACES_API_KEY=...")
        print("  (Google Cloud Console -> enable 'Places API (New)' -> create a key)")
        return 1

    print(f"\nSweeping (hard stop at {args.max_calls} calls)...")
    try:
        cands, stats = google_places.discover(
            key, radius_km=args.radius, tile_km=args.tile_km,
            max_calls=args.max_calls)
    except RuntimeError as exc:
        print(f"\n  STOPPED: {exc}")
        return 1

    print(f"\n  calls made        {stats['calls']}  (~${stats['estimated_usd']})")
    print(f"  tiles visited     {stats['tiles_done']}/{stats['tiles_total']}"
          + ("  ⚠ budget exhausted, coverage PARTIAL" if stats["budget_exhausted"] else ""))
    print(f"  tiles subdivided  {stats['subdivided']}  (came back full)")
    print(f"  unique companies  {stats['unique_companies']}")

    kept = [c for c in cands if filters.accept(c.name)]
    print(f"  after junk filter {len(kept)}")

    db = SessionLocal()
    try:
        existing = [(r.name, r.city, (r.lat, r.lon) if r.lat is not None else None)
                    for r in db.query(RegionalCompany).all()]
        new, known = matcher.split_new_vs_known(kept, existing)
        print(f"\n  already in register {len(known)}")
        print(f"  NEW                 {len(new)}")

        print(f"\n{'='*74}\nNEW from Google Places (nearest first)\n{'='*74}")
        for c in sorted(new, key=lambda x: (x.distance_km is None, x.distance_km or 0))[:50]:
            d = f"{c.distance_km:.0f}km" if c.distance_km is not None else "  ? "
            print(f"  {d:>6}  {c.name[:42]:<42} {(c.city or '?')[:18]:<18} {c.branche or ''}")
        if len(new) > 50:
            print(f"          ... and {len(new)-50} more")

        if args.apply:
            for c in new:
                db.add(RegionalCompany(
                    name=c.name, normalized_name=normalize_company_name(c.name),
                    city=c.city, lat=c.lat, lon=c.lon,
                    distance_km=c.distance_km,
                    in_radius=(c.distance_km is not None and c.distance_km <= args.radius),
                    branche=c.branche, source="google_places", source_url=c.source_url,
                    postcode=(c.raw or {}).get("postcode"),
                    address=(c.raw or {}).get("address"),
                    triage_tier=triage.tier_for(employees=None, source="google_places"),
                    raw={**(c.raw or {}), "discovered_at": datetime.utcnow().isoformat()},
                ))
            db.commit()
            print(f"\n  ✓ wrote {len(new)} new companies")
        else:
            print(f"\n  Not written (add --apply). API calls WERE made and charged.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
