"""
Import the "Potenzielle Mitglieder" sheet into the regional register (RC-1).

Dry-run by default — prints exactly what would change and commits nothing.
Re-run with --apply once the preview looks right. Same discipline as
scripts/dedup_sweep.py.

Prepare the input by exporting your Excel sheet to CSV. Expected headers
(German, as in the sheet; matching ignores case, spaces and umlauts):

    Name, Standort, Mitarbeiter, Branche, Kurzbeschreibung,
    Prio, Kontakt allgemein, Status, Phase, Wer hat Kontakt

Only `Name` is required. Put the file somewhere under regional/data/ — that
directory is gitignored, which matters because the CRM columns contain named
individuals' phone numbers and e-mail addresses.

Usage:
    python3 scripts/rc_import_sheet.py --csv regional/data/mitglieder.csv
    python3 scripts/rc_import_sheet.py --csv regional/data/mitglieder.csv --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal  # noqa: E402
from regional.importer import import_rows, parse_csv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Import the membership sheet (RC-1)")
    ap.add_argument("--csv", required=True, help="path to the exported CSV")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"No such file: {args.csv}")
        return 1

    print(f"Reading {args.csv} ...")
    rows = parse_csv(args.csv)
    if not rows:
        print("No rows with a usable 'Name' column found — check the headers.")
        return 1
    print(f"  parsed {len(rows)} companies")

    with_city = sum(1 for r in rows if r.get("city"))
    print(f"  {with_city} have a Standort and will be geocoded "
          f"(~1s each on first run, cached afterwards)\n")

    db = SessionLocal()
    try:
        stats = import_rows(db, rows, apply=args.apply)
    finally:
        db.close()

    mode = "APPLIED" if args.apply else "DRY RUN — nothing written"
    print(f"\n{'='*66}\n{mode}\n{'='*66}")
    print(f"  parsed           {stats['parsed']}")
    print(f"  would insert     {stats['inserted']}" if not args.apply
          else f"  inserted         {stats['inserted']}")
    print(f"  would update     {stats['updated']}" if not args.apply
          else f"  updated          {stats['updated']}")
    print(f"  inside 50 km     {stats['in_radius']}")
    print(f"  outside 50 km    {stats['out_of_radius']}   (kept, flagged)")
    if stats["geocode_failed"]:
        print(f"  geocode failed   {stats['geocode_failed']}")
    if stats["skipped_no_city"]:
        print(f"  no Standort      {stats['skipped_no_city']}")

    if stats["unresolved"]:
        print(f"\n  ⚠  {len(stats['unresolved'])} Standort(e) could not be located "
              f"— fix these in the source sheet:")
        for u in stats["unresolved"]:
            print(f"     - {u}")

    if stats["warnings"]:
        print(f"\n  ⚠  {len(stats['warnings'])} location(s) look wrong:")
        for w in stats["warnings"]:
            print(f"     - {w}")

    if not args.apply:
        print("\n  Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
