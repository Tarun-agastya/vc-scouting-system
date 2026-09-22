"""
Delete records that are not companies at all. Dry run by default, per class.

Why this is narrower than it first looks
----------------------------------------
Running the duplicate adjudicator on 22 Sep showed that roughly a third of the
pairs it was asked to judge were not companies: it was carefully deciding
whether two *events* were the same event. The junk classes below are what it
surfaced.

The obvious fourth class was deliberately dropped after checking it. 358
records have no website and no description, which looks like the biggest junk
class in the database — and is not junk at all. They are bare-name discoveries
from logo grids and portfolio pages, exactly the shape this pipeline is
designed to capture and enrich later: "Basemate" and "Matestack" from
zollhof.de's portfolio, "Argá Medtech" from High-Tech Gründerfonds, "Gate" and
"YFN" from munich-startup.de. **Deleting them would destroy genuine leads**,
so absence of evidence is never a deletion criterion here.

What each class is
------------------
  categories  Generic plural nouns and topic labels that a listing page used
              as a heading — "Robotik-Startups" (description: "Münchner
              Robotik-Startups und ihre Lösungen"), "Traveltech-Startups",
              "Deutsche Startup-Landschaft", "Unternehmen". Never a company.
  events      A named event with a year — "Munich Startup Festival 2024",
              "STARTUP TEENS-Ideen-Camp". Checked individually, because a
              company can legitimately carry a year.
  ambiguous   REPORT ONLY, never deletable. A bare two-word capitalised name
              with no website and no description. This began as a "people"
              class and the first dry run killed the idea: the same pattern
              matches "Astra Labs", "Dallara Automobili", "Bloom Partners" and
              "Cometa Kempten", which are companies, alongside genuine bylines
              like "Eric Archambeau" and "Christian Willert". No regex
              separates those two groups, so this bucket is printed for
              processing/dedup_adjudicator.py or a human, and --apply refuses
              to touch it.

Each deletable class is opted into separately, and nothing is deleted until
you have seen the list. An accelerator or a fund is NOT in scope: BayStartUP and
Venturelab are real organisations, merely not investment targets, and deciding
those is a different conversation from deleting a page heading.

Usage
-----
    python3 scripts/purge_non_companies.py                      # show all
    python3 scripts/purge_non_companies.py --classes categories
    python3 scripts/purge_non_companies.py --classes categories,events --apply
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch

# Whole-name generic labels, and topic headings ending in a plural category.
_GENERIC_EXACT = {
    "unternehmen", "startups", "start-ups", "firmen", "companies",
    "startup", "unternehmensliste", "mitglieder", "partner", "portfolio",
}
_CATEGORY_RE = re.compile(
    r"(^|\s|-)("
    r"[\wÀ-ɏ]+-startups?"          # Robotik-Startups, Traveltech-Startups
    r"|startup-landschaft"
    r"|startup-szene"
    r")(\s|$)",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(r"\b(19|20)\d{2}\b")
_PERSON_RE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]{1,}\s+[A-ZÄÖÜ][a-zäöüß]{1,}$")


def _classify(s) -> str:
    name = (s.name or "").strip()
    low = name.lower()
    has_site = bool((s.website or "").strip())
    has_text = bool((s.description or "").strip() or (s.short_description or "").strip())

    if low in _GENERIC_EXACT or _CATEGORY_RE.search(name):
        return "categories"
    if _EVENT_RE.search(name):
        return "events"
    # Only with zero corroborating evidence. A person's name attached to a
    # real website is far more likely a one-person company than a byline.
    if _PERSON_RE.match(name) and not has_site and not has_text:
        return "ambiguous"
    return ""


def run(classes: set, apply: bool) -> None:
    db = SessionLocal()
    try:
        buckets = {"categories": [], "events": [], "ambiguous": []}
        for s in db.query(Startup).all():
            c = _classify(s)
            if c:
                buckets[c].append(s)

        for name, rows in buckets.items():
            selected = name in classes and name != "ambiguous"
            tag = ("   [SELECTED]" if selected
                   else "   — REPORT ONLY, cannot be deleted" if name == "ambiguous"
                   else "   — not selected")
            print(f"\n=== {name}  ({len(rows)} records){tag}")
            for s in sorted(rows, key=lambda r: (r.name or ""))[:25]:
                ev = "website" if (s.website or "").strip() else (
                    "text" if (s.description or s.short_description) else "no evidence")
                print(f"    {(s.name or '')[:46]:48} {ev}")
            if len(rows) > 25:
                print(f"    … and {len(rows) - 25} more")

        classes = {c for c in classes if c != "ambiguous"}
        total = sum(len(buckets[c]) for c in classes)
        print(f"\nSelected for deletion: {total} records")
        print("NOT touched: 358 bare-name records with no website or description —")
        print("those are real leads awaiting enrichment, not junk. See this file's docstring.")

        if not apply:
            print("\nDry run — nothing deleted. Add --apply once the lists look right.")
            return
        if not classes:
            print("\nNo --classes given, so there is nothing to delete.")
            return

        from vector_db.qdrant_store import qdrant_store
        deleted = refs = 0
        for c in classes:
            for s in buckets[c]:
                refs += db.query(DuplicateReview).filter(
                    (DuplicateReview.master_id == s.id) | (DuplicateReview.incoming_id == s.id)
                ).delete(synchronize_session=False)
                refs += db.query(SuppressedMatch).filter(
                    (SuppressedMatch.master_id == s.id) | (SuppressedMatch.other_id == s.id)
                ).delete(synchronize_session=False)
                try:
                    qdrant_store.delete_startup(str(s.id))
                except Exception:
                    pass
                db.delete(s)
                deleted += 1
        db.commit()
        print(f"\nDeleted {deleted} records and {refs} reviews/suppressions referencing them.")
        print(f"Records remaining: {db.query(Startup).count()}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classes", default="",
                    help="comma-separated: categories,events  (ambiguous is report-only)")
    ap.add_argument("--apply", action="store_true", help="actually delete")
    args = ap.parse_args()
    chosen = {c.strip() for c in args.classes.split(",") if c.strip()}
    if "ambiguous" in chosen:
        print("'ambiguous' is report-only by design — see this file's docstring.")
        sys.exit(2)
    bad = chosen - {"categories", "events"}
    if bad:
        print(f"Unknown class(es): {', '.join(sorted(bad))}")
        sys.exit(2)
    run(chosen, args.apply)


if __name__ == "__main__":
    main()
