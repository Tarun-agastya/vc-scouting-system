"""
Measure OSM building footprints as a free size proxy (Phase RC-3b).

WHY. The 978 OSM-sourced companies would cost ~27 hours of local GPU to
enrich, and most are one-person workshops — enriching them mostly establishes
that sole traders are sole traders. Footprint area separates them for free,
with no LLM call and no API cost.

Measured on 342 real buildings (7 Aug), and the separation is unambiguous:

    405,000 m²  Grob-Werke              one of Germany's largest machine-tool
    349,000 m²  Kögel Trailer            makers, and NOT on the membership
    347,000 m²  Peri GmbH                sheet nor sized by any other source
    289,000 m²  Daimler Buses Werk 5
    148,000 m²  Max Weishaupt
    ...
        118 m²  IP-Hartmann Werbetechnik
         95 m²  easy-page werbedesign
         81 m²  "Büro"
         64 m²  design schmid

Companies above the threshold are promoted into the enrichment queue; the rest
stay low-prior. A NULL footprint (an OSM node with no mapped building) is
never read as "small" — unknown stays unknown.

Dry run by default.

Usage:
    python3 scripts/rc_footprints.py
    python3 scripts/rc_footprints.py --apply
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal        # noqa: E402
from database.models import RegionalCompany         # noqa: E402
from regional import triage                         # noqa: E402
from regional.discovery import OVERPASS_MIRRORS, _USER_AGENT  # noqa: E402
from regional.geometry import polygon_area_m2        # noqa: E402

BATCH = 150


def _fetch(way_ids) -> dict:
    q = f"[out:json][timeout:180];way(id:{','.join(map(str, way_ids))});out geom;"
    body = urllib.parse.urlencode({"data": q}).encode()
    for mirror in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(mirror, data=body,
                                         headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as r:
                payload = json.load(r)
            if payload.get("elements"):
                return payload
        except Exception as exc:
            print(f"  mirror {mirror} failed: {exc}")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="OSM footprint size proxy (RC-3b)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--threshold", type=float, default=triage.LARGE_FOOTPRINT_M2)
    ap.add_argument("--batch", type=int, default=BATCH,
                    help="way ids per Overpass call (lower if it times out)")
    ap.add_argument("--all", action="store_true",
                    help="re-measure everything, not just the unmeasured")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(RegionalCompany).filter(RegionalCompany.in_radius.is_(True))
        if not args.all:
            # Incremental by default. Overpass returns 504 on roughly a third
            # of batches, so a re-run must pick up where the last one stopped
            # rather than re-fetching everything and timing out again.
            q = q.filter(RegionalCompany.footprint_m2.is_(None))
        rows = [c for c in q.all()
                if c.source_url and "/way/" in (c.source_url or "")]
        by_id = {}
        for c in rows:
            try:
                by_id[int(c.source_url.rstrip("/").split("/")[-1])] = c
            except ValueError:
                pass
        print(f"companies with a mapped OSM building: {len(by_id)}")
        if not by_id:
            print("  nothing to measure")
            return 0

        ids = list(by_id)
        measured = 0
        for i in range(0, len(ids), args.batch):
            payload = _fetch(ids[i:i + args.batch])
            for el in payload.get("elements", []):
                geom = el.get("geometry")
                comp = by_id.get(el["id"])
                if geom and comp is not None:
                    comp.footprint_m2 = round(polygon_area_m2(geom), 1)
                    measured += 1
            print(f"  measured {measured}/{len(ids)}")

        db.flush()
        allrows = db.query(RegionalCompany).filter(
            RegionalCompany.in_radius.is_(True),
            RegionalCompany.footprint_m2.isnot(None)).all()
        sized = allrows
        big = [c for c in sized if (c.footprint_m2 or 0) >= args.threshold]
        promotable = [c for c in big
                      if c.employees is None and c.triage_tier == triage.TIER_LOW_PRIOR]

        print(f"\n  with a measured footprint  {len(sized)}")
        print(f"  >= {args.threshold:,.0f} m²             {len(big)}")
        print(f"  promotable to enrich queue {len(promotable)}"
              f"   (~{len(promotable)*100/3600:.1f}h GPU, vs "
              f"~{db.query(RegionalCompany).filter(RegionalCompany.in_radius.is_(True), RegionalCompany.employees.is_(None)).count()*100/3600:.0f}h for all)")

        print(f"\n  largest 15 being promoted:")
        for c in sorted(promotable, key=lambda x: -(x.footprint_m2 or 0))[:15]:
            print(f"    {c.footprint_m2:>10,.0f} m²  {c.name[:42]:<42} {(c.city or '?')[:18]}")

        if args.apply:
            for c in promotable:
                c.triage_tier = triage.tier_for(employees=c.employees, source=c.source,
                                                footprint_m2=c.footprint_m2)
            db.commit()
            print(f"\n  ✓ stored {measured} footprints, promoted {len(promotable)} "
                  f"into the enrichment queue")
        else:
            db.rollback()
            print(f"\n  DRY RUN — nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
