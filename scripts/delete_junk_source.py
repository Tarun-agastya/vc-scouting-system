"""
One-off cleanup: delete confirmed-junk startup records from a given source
domain, plus every DuplicateReview/SuppressedMatch row that references them,
plus their Qdrant points.

Built 4 Aug 2026 for the hochschule-biberach.de incident (see
DATA_INTEGRITY_PLAN.md's review-inbox-flooding writeup and
ingestion/web_scraper.py's SKIP_PATTERNS/_drop_numbered_sequences fix) — a
crawl wandered into off-topic university pages (professor recruitment,
governance, cafeteria, IT department, a partner-logo "our network" page) and
extracted their photo captions / established-firm names as ~124 fake
"startups", all with zero real evidence (no description, no website).

Safety: only ever deletes records where BOTH description and website are
empty — a record with any real evidence is left alone and reported instead
of deleted, so this can't accidentally remove a genuine startup that merely
shares a source domain with junk. Dry run by default; --apply to execute.
Mirrors scripts/dedup_sweep.py's dry-run/--apply convention.

Second incident, same domain (16 Sep 2026)
------------------------------------------
The registered Gründung URL had started 404ing, and with no valid entry page
the crawl wandered into /studium/bachelorstudium/* and stored 11 more fake
startups — but this time they were NOT caught by the rule above, because
each one carried a `website`. Inspecting them showed every such website was
a `/kontakt/<person-name>` staff profile on the university's own domain: the
extractor had taken each professor's subject area as the company name
("Energierecht", "Baukonstruktion und Entwerfen") and their staff page as
the company site.

So the original rule was right in principle and too narrow in practice — a
URL is only *evidence of a company* if it points somewhere other than a
person's page on the source's own site. `--staff-pages-are-not-evidence`
adds exactly that criterion and nothing more: same-domain person/contact
pages stop counting as evidence, everything else still protects a record.
It stays opt-in because it is a judgement about one shape of page, and a
future caller should have to think about whether it applies to their domain.

Usage
-----
    python scripts/delete_junk_source.py --domain hochschule-biberach.de
    python scripts/delete_junk_source.py --domain hochschule-biberach.de --apply
    python scripts/delete_junk_source.py --domain hochschule-biberach.de \
        --staff-pages-are-not-evidence          # dry run first, as always
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup, DuplicateReview, SuppressedMatch

# Path segments that mark a URL as a page ABOUT A PERSON rather than a
# company: a staff directory entry, a team bio, an author page. Matched only
# against URLs on the source's own domain — the same path on a company's own
# site ("acme.com/team") is perfectly normal and must keep counting as
# evidence.
_STAFF_PATH_MARKERS = (
    "/kontakt/", "/contact/", "/team/", "/personen/", "/person/",
    "/mitarbeiter", "/staff/", "/autor/", "/author/", "/profil/",
    "/ansprechpartner",
)


def _is_staff_page_on_own_domain(website: str, domain: str) -> bool:
    """
    True when `website` is a person-shaped page on the source domain itself,
    which is not independent evidence that a company exists.
    """
    if not website:
        return False
    w = website.lower()
    if domain.lower() not in w:
        return False
    return any(marker in w for marker in _STAFF_PATH_MARKERS)


def run(domain: str, apply: bool, staff_pages_are_not_evidence: bool = False) -> None:
    db = SessionLocal()
    try:
        rows = db.query(Startup).filter(Startup.source_url.ilike(f"%{domain}%")).all()
        if not rows:
            print(f"No records found for domain '{domain}'.")
            return

        def has_evidence(r) -> bool:
            if r.description:
                return True
            if not r.website:
                return False
            if staff_pages_are_not_evidence and _is_staff_page_on_own_domain(r.website, domain):
                return False
            return True

        junk = [r for r in rows if not has_evidence(r)]
        keep = [r for r in rows if r not in junk]

        criterion = "no description, no website"
        if staff_pages_are_not_evidence:
            criterion += ", or only a same-domain staff page as 'website'"
        print(f"Domain: {domain}")
        print(f"Total records: {len(rows)}")
        print(f"Junk ({criterion}) — would delete: {len(junk)}")
        for r in junk:
            why = f"  (website={r.website})" if r.website else ""
            print(f"  DELETE  {r.id}  {r.name!r}  <- {r.source_url}{why}")
        if keep:
            print(f"Has real evidence — would KEEP (not touched): {len(keep)}")
            for r in keep:
                print(f"  KEEP    {r.id}  {r.name!r}")

        if not apply:
            print("\nDry run only — nothing deleted. Re-run with --apply to execute.")
            return

        from vector_db.qdrant_store import qdrant_store

        ids = [str(r.id) for r in junk]
        reviews_deleted = db.query(DuplicateReview).filter(
            (DuplicateReview.master_id.in_(ids)) | (DuplicateReview.incoming_id.in_(ids))
        ).delete(synchronize_session=False)
        suppressions_deleted = db.query(SuppressedMatch).filter(
            (SuppressedMatch.master_id.in_(ids)) | (SuppressedMatch.other_id.in_(ids))
        ).delete(synchronize_session=False)

        for r in junk:
            db.delete(r)
        db.commit()

        qdrant_failures = 0
        for i in ids:
            try:
                qdrant_store.delete_startup(i)
            except Exception:
                qdrant_failures += 1

        print(f"\nDeleted {len(junk)} startup rows, {reviews_deleted} reviews, "
              f"{suppressions_deleted} suppressions.")
        print(f"Qdrant point deletes attempted: {len(ids)}, failures: {qdrant_failures}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, help="Source domain to clean up, e.g. hochschule-biberach.de")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default is dry run)")
    ap.add_argument("--staff-pages-are-not-evidence", action="store_true",
                    help="Treat a same-domain /kontakt//team/ person page as NOT evidence "
                         "of a company (see the second-incident note in this file's docstring)")
    args = ap.parse_args()
    run(args.domain, args.apply, args.staff_pages_are_not_evidence)


if __name__ == "__main__":
    main()
