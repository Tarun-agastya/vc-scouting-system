"""
One-off cleanup: delete confirmed-junk startup records from two NEW signal
classes found live 14 Aug 2026 that scripts/delete_implausible_names.py's
NAME-only checks don't cover — a page-structure artifact and a source-
content-integrity failure, not a naming pattern.

WHY THIS EXISTS SEPARATELY. Owner-reported noise in the Review Inbox and DB
("names of websites, complete noise... not really startups"). Investigating
found several distinct classes; the name-pattern ones (known incumbents,
person names with a title, e.g. "SCE-Chef Klaus Sailer") were folded into
scripts/delete_implausible_names.py's own config (config/tuning.yaml's
institutional_junk.known_incumbents / .person_title_pattern) since that
script's machinery already exists for exactly that shape of problem — re-run
IT to pick those up. The two classes here needed new machinery instead:

1. BARE CATEGORY LABELS — a page section heading ("Healthtech Startups",
   "Enterprise Software Startups") describing MULTIPLE companies extracted
   as if it were itself one company. Confirmed by reading the actual
   description on every hit before shipping this: even the ones with
   populated content read as "Four Munich-based startups are highlighted…"
   / "The Enterprise Software sector in Munich includes over 270
   startups…" — a section summary, never a single company's own
   description. Detected via --name-only: a name matches
   `\\b(startups?|gr[üu]nder\\w*)$`.

2. CIPHERED SOURCE CONTENT — sifted.eu serves some article pages (paywalled
   "Pro Exclusive" content specifically, confirmed by inspection) with a
   monoalphabetic-substitution-ciphered body: readable HTML structure and
   punctuation preserved, but every letter substituted, including inside
   URLs and tag names ("<a href=" becomes "<m dbyr=", "https://" becomes
   "wdgkv://"). The extraction pipeline had no way to know the text it was
   reading was gibberish and dutifully "extracted" plausible-shaped fake
   company names from it (e.g. "Npiqxwxm", "Achaiez Guqzvol"). Detected via
   a fake URL scheme in source_excerpt (a scheme string before "://" that
   isn't http/https/ftp/mailto/tel/data/ws/wss — never happens in genuine
   scraped content; verified zero false positives across ALL 1978 rows with
   a source_excerpt outside sifted.eu). NOT every record from an affected
   article is fake, though — some articles have a legible free teaser above
   the ciphered paywalled continuation, and a name extracted from the
   teaser is still real (confirmed live: "Mistral" and "Pliant" are real,
   correctly-identified companies whose OWN article also happens to contain
   ciphered paywalled text elsewhere). Distinguished by checking the LOCAL
   TEXT immediately around each name's own occurrence in source_excerpt —
   genuine prose there clears a plain-language stopword-density bar easily;
   ciphered text never does. Manually read and hand-verified every one of
   the 22 real candidates before trusting this split; matched exactly
   (19 confirmed junk, 3 confirmed real: Wordsmith, Mistral, Pliant).

Both checks are opt-in via flags so a re-run months from now doesn't have to
mean "yes to everything found today" — see usage.

Also deletes every DuplicateReview/SuppressedMatch referencing a matched
record, plus its Qdrant point — same cleanup shape as
delete_implausible_names.py. Dry run by default.

Usage
-----
    python scripts/delete_noise_startups.py --name-only                 # bare category labels, dry run
    python scripts/delete_noise_startups.py --ciphered-source           # ciphered-content junk, dry run
    python scripts/delete_noise_startups.py --name-only --ciphered-source --apply
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal      # noqa: E402
from database.models import Startup, DuplicateReview, SuppressedMatch  # noqa: E402

_BARE_COLLECTIVE = re.compile(r"\b(startups?|gr[üu]nder(?:innen|in)?(?:zentrum|szene)?)$", re.IGNORECASE)

_REAL_URL_SCHEMES = {"http", "https", "ftp", "mailto", "tel", "data", "ws", "wss"}
_FAKE_SCHEME_RE = re.compile(r"\b([a-z]{2,8})://")

_STOPWORDS = {
    "the", "and", "is", "a", "to", "of", "in", "for", "on", "with", "has", "said", "was", "are",
    "it", "its", "that", "this", "by", "at", "be", "as", "from", "now", "following", "raises",
    "der", "die", "das", "und", "ist", "für", "mit", "von", "auf", "hat", "war", "sind", "ein",
    "eine", "den", "dem",
}


# One specific, hand-reviewed exclusion (14 Aug 2026): every other
# bare-collective match's description reads as a SECTION SUMMARY about
# multiple companies ("Four Munich-based startups are highlighted…", "The
# Enterprise Software sector in Munich includes over 270 startups…") — never
# a single company's own description. "Dehaze Health-AI-Startup" is the one
# exception: "Health AI startup focusing on chronic disease recognition"
# reads like a genuine singular-company description with a malformed NAME
# (the real name is plausibly just "Dehaze", with the category wrongly
# concatenated onto it) rather than a genuinely fake record. Left alone
# pending a human decision — rename vs delete — rather than guessed at
# either way.
_NAME_EXCLUSIONS = {"Dehaze Health-AI-Startup"}


def _is_bare_collective_name(name: str) -> bool:
    stripped = (name or "").strip()
    if stripped in _NAME_EXCLUSIONS:
        return False
    return bool(_BARE_COLLECTIVE.search(stripped))


def _has_fake_url_scheme(text: str) -> bool:
    return any(m.group(1) not in _REAL_URL_SCHEMES for m in _FAKE_SCHEME_RE.finditer(text or ""))


def _local_context_is_legible(name: str, excerpt: str, window: int = 150) -> bool:
    """
    Is the text immediately around THIS name's own occurrence real prose?
    Checked separately from the whole excerpt's average, because a single
    article can mix a legible free teaser with ciphered paywalled text —
    see the module docstring's "Mistral"/"Pliant" case.
    """
    first_tok = (name or "").split()[0] if name else ""
    if not first_tok:
        return False
    idx = (excerpt or "").find(first_tok)
    if idx < 0:
        return False  # doesn't even appear verbatim in its own source -- can't vouch for it
    before = excerpt[max(0, idx - window):idx]
    after = excerpt[idx + len(first_tok):idx + len(first_tok) + window].split("\n")[0]
    words = re.findall(r"[a-zA-ZäöüÄÖÜß]+", (before + " " + after).lower())
    if len(words) < 6:
        return False
    return (sum(1 for w in words if w in _STOPWORDS) / len(words)) >= 0.10


def _is_ciphered_source_junk(row) -> bool:
    if not _has_fake_url_scheme(row.source_excerpt):
        return False
    return not _local_context_is_legible(row.name, row.source_excerpt)


def run(*, name_only: bool, ciphered_source: bool, apply: bool) -> None:
    db = SessionLocal()
    try:
        rows = db.query(Startup).all()
        print(f"Total records scanned: {len(rows)}")

        junk = []
        if name_only:
            hits = [r for r in rows if _is_bare_collective_name(r.name)]
            print(f"\nBare category-label names — {len(hits)}:")
            for r in hits:
                desc = (r.description or r.short_description or "")[:90]
                print(f"  DELETE  {r.id}  {r.name!r:35} desc={desc!r}")
            junk.extend(hits)

        if ciphered_source:
            hits = [r for r in rows if r.source_excerpt and _is_ciphered_source_junk(r)]
            print(f"\nCiphered-source-content junk (sifted.eu paywall cipher) — {len(hits)}:")
            for r in hits:
                print(f"  DELETE  {r.id}  {r.name!r:35} <- {r.source_url}")
            junk.extend(hits)

        if not name_only and not ciphered_source:
            print("\nNothing to do — pass --name-only and/or --ciphered-source.")
            return

        print(f"\nTotal to delete: {len(junk)}")
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Delete bare-category-label and ciphered-source-content junk")
    ap.add_argument("--name-only", action="store_true", help="Delete bare category-label names (e.g. 'Healthtech Startups')")
    ap.add_argument("--ciphered-source", action="store_true", help="Delete names extracted from confirmed-ciphered source content")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    run(name_only=args.name_only, ciphered_source=args.ciphered_source, apply=args.apply)
