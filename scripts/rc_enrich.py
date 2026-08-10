"""
Enrich regional companies from live web search (Phase RC-4).

Fills Mitarbeiter / Branche / Kurzbeschreibung / Website, recording a source
URL per field. Works the triage-tier 2 queue by default — the companies that
came from a notability-filtered source but have no headcount yet.

COST: each company costs ONE outbound search plus ONE local 14B call
(~30-180s). The batch limit is the only cost control that exists —
ingestion/web_search.py has no quota accounting of any kind — so --limit
defaults deliberately low. Start small, look at the output, then widen.

Runs one company at a time under the GPU mutex; safe to run while the
scouting pipeline is idle, and it will queue rather than collide if not.

Usage:
    python3 scripts/rc_enrich.py --limit 5              # dry run, 5 companies
    python3 scripts/rc_enrich.py --limit 20 --apply
    python3 scripts/rc_enrich.py --tier 1 --limit 10    # refresh known ones
    python3 scripts/rc_enrich.py --name "Otto Christ" --apply
    python3 scripts/rc_enrich.py --all-tiers --limit 1000 --apply
        # every in-radius company with no Kurzbeschreibung yet, tier ignored —
        # for "at least get a description on everyone", including the ~875
        # tier-3 companies the tiering deliberately deprioritises for
        # headcount (a description is a much cheaper thing to ask the same
        # call for, so there is no reason to withhold it from tier 3 too).
"""
import argparse
import asyncio
import functools
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal      # noqa: E402
from database.models import RegionalCompany       # noqa: E402
from regional import enrich, triage               # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")

# Each company takes 30-180s, so a batch of 60 runs for hours. Without this,
# stdout stays block-buffered when redirected to a log file and the run looks
# dead until it finishes — confirmed 7 Aug watching an empty log for minutes
# while the job was in fact working.
print = functools.partial(print, flush=True)  # noqa: A001


def _select(db, args):
    q = db.query(RegionalCompany).filter(RegionalCompany.in_radius.is_(True))
    if args.name:
        return q.filter(RegionalCompany.name.ilike(f"%{args.name}%")).all()

    if args.all_tiers:
        # Ignores the triage tier entirely — the tiering exists to gate the
        # EXPENSIVE, low-yield hunt for a headcount on tier-3's ~875 mostly
        # small OSM finds, not to withhold a description from them too. Every
        # enrich_one() call already asks for Mitarbeiter/Branche/
        # Kurzbeschreibung/Website in one shot, so running it here costs the
        # same per company and simply keeps whatever it happens to find
        # (usually the description, per the measured 83%/49%/36% hit rates on
        # Branche/Kurzbeschreibung/Mitarbeiter — description is NOT the
        # highest-yield field, but this is "at least try everyone" mode).
        q = q.filter(RegionalCompany.kurzbeschreibung.is_(None))
    else:
        q = q.filter(RegionalCompany.triage_tier == args.tier)
        if args.tier == triage.TIER_ENRICH:
            q = q.filter(RegionalCompany.employees.is_(None))

    # Never-attempted first, then nearest. A company nobody has looked at is
    # always worth more than re-running one the web had nothing on, and a
    # company 3 km away is a better prospect than one 48 km away — so when the
    # batch limit cuts the queue, it cuts the already-tried and the far end.
    return (q.order_by(RegionalCompany.last_verified_at.asc().nullsfirst(),
                       RegionalCompany.distance_km.asc().nullslast())
             .limit(args.limit).all())


async def run(args) -> int:
    db = SessionLocal()
    try:
        targets = _select(db, args)
        if not targets:
            print("Nothing matches that selection.")
            return 0

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"{mode}: enriching {len(targets)} companies "
              f"(1 search + 1 local LLM call each)\n")

        stats = {"processed": 0, "no_result": 0,
                 "filled": 0, "proposed_only": 0, "employees_found": 0}

        for i, company in enumerate(targets, 1):
            d = f"{company.distance_km:.0f}km" if company.distance_km is not None else "?"
            print(f"[{i}/{len(targets)}] {company.name}  ({company.city or '?'}, {d})")

            proposals = await enrich.enrich_one(company)
            stats["processed"] += 1
            if not proposals:
                print("      no usable findings")
                stats["no_result"] += 1
                if args.apply:
                    # Record the ATTEMPT even though it yielded nothing.
                    # Without this, "tried and the web has no headcount for
                    # this firm" is indistinguishable from "never tried", and
                    # every later batch re-spends a search and a 100-second
                    # LLM call on the same dead ends before reaching anything
                    # new. Combined with the oldest-attempt-first ordering in
                    # _select, a failure is retried eventually but never ahead
                    # of a company nobody has looked at yet.
                    company.last_verified_at = datetime.utcnow()
                    db.commit()
                continue

            shown = 0
            for field, p in proposals.items():
                cur = getattr(company, field, None)
                val = str(p["value"])
                if cur in (None, "", 0):
                    marker = "fill   "
                elif str(cur).strip() == val.strip():
                    # The model confirmed what we already had. Real and useful,
                    # but not news — don't dress it up as a conflict.
                    continue
                else:
                    marker = "differs"
                shown += 1
                print(f"      {marker} {field:<17} {val[:58]}")
                if p["source_url"]:
                    print(f"              source: {p['source_url'][:70]}")
            if not shown:
                print("      confirmed existing values, nothing new")

            if args.apply:
                res = enrich.apply_proposals(company, proposals)
                stats["filled"] += len(res["filled"])
                stats["proposed_only"] += len(res["proposed_only"])
                if "employees" in res["filled"]:
                    stats["employees_found"] += 1
                    company.triage_tier = triage.tier_for(
                        employees=company.employees, source=company.source)
                db.commit()

        print(f"\n{'='*70}")
        print(f"{mode} complete")
        print(f"{'='*70}")
        print(f"  processed          {stats['processed']}")
        print(f"  no usable findings {stats['no_result']}")
        if args.apply:
            print(f"  fields filled      {stats['filled']}")
            print(f"  headcounts found   {stats['employees_found']}")
            print(f"  differed (kept for a human, not overwritten) {stats['proposed_only']}")
        else:
            print("\n  Nothing written. Re-run with --apply.")
    finally:
        db.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich regional companies (RC-4)")
    ap.add_argument("--limit", type=int, default=5,
                    help="max companies this run (default 5 — each costs a "
                         "search + an LLM call)")
    ap.add_argument("--tier", type=int, default=triage.TIER_ENRICH,
                    help=f"triage tier to work (default {triage.TIER_ENRICH}); "
                         f"ignored if --all-tiers is set")
    ap.add_argument("--all-tiers", action="store_true",
                    help="ignore tier — target every in-radius company with "
                         "no Kurzbeschreibung yet (includes tier 3)")
    ap.add_argument("--name", help="enrich companies matching this name instead")
    ap.add_argument("--apply", action="store_true", help="write results")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
