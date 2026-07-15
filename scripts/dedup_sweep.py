"""
One-time duplicate cleanup sweep — Revision 2 requirement #1.

Finds startups already sitting in the database as duplicates from earlier
ingestion runs (before fingerprint + fuzzy-match dedup was fully wired into
every write path) and merges them into a single canonical row, removing the
losers from both PostgreSQL and Qdrant.

This is separate from the ONGOING dedup that already happens on every
upsert_startup() call — this script is a one-time backfill for whatever
duplicates accumulated before that was airtight.

Grouping strategy
------------------
1. Exact fingerprint match (same normalized name + domain) — guaranteed
   duplicates, always safe to merge.
2. Fuzzy name match (rapidfuzz token_sort_ratio >= 88 — the same threshold
   used by the live dedup path in processing/deduplicator.py) across the
   whole table. Catches the same company surfaced with a different domain
   or a slightly different name spelling. All-pairs comparison is fine at
   this table size; a blocking strategy would be worth adding if the table
   grows into the thousands.

Merge rule (reuses processing/storage.py::_fill_empty_fields — the same
"never overwrite populated data" rule live ingestion already follows)
----------------------------------------------------------------------
- The OLDEST row (earliest created_at) in each group is the keeper — its
  stable UUID stays the canonical identity going forward.
- Every empty field on the keeper is filled from the losers.
- Every loser's source_history entries are appended to the keeper's
  (deduplicated by URL).
- Tags are unioned (via _fill_empty_fields's existing tag-merge logic).
- Losers are deleted from PostgreSQL and Qdrant.
- The keeper is re-embedded and re-upserted to Qdrant with the merged data.

Usage
-----
    python scripts/dedup_sweep.py             # dry run: prints the merge plan, writes nothing
    python scripts/dedup_sweep.py --apply     # executes the merge for real
"""
import sys
import os
import argparse
import logging
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88


def _find_groups(rows):
    """
    Group rows that represent the same real-world startup via union-find,
    so a fingerprint match and a fuzzy match that share a row end up in one
    combined group (e.g. A~B by fingerprint, B~C by fuzzy name -> {A,B,C}).

    Returns only groups with 2+ rows (singletons are not duplicates).
    """
    from processing.deduplicator import normalize_company_name
    from rapidfuzz import fuzz

    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # 1. Exact fingerprint match
    by_fingerprint = defaultdict(list)
    for i, row in enumerate(rows):
        if row.fingerprint:
            by_fingerprint[row.fingerprint].append(i)
    for indices in by_fingerprint.values():
        for idx in indices[1:]:
            union(indices[0], idx)

    # 2. Fuzzy name match — skip very short names to avoid false positives
    #    on generic words (mirrors fuzzy_match_existing's own guard).
    normalized = [normalize_company_name(r.name) for r in rows]
    for i in range(len(rows)):
        if len(normalized[i]) < 4:
            continue
        for j in range(i + 1, len(rows)):
            if find(i) == find(j) or len(normalized[j]) < 4:
                continue
            if fuzz.token_sort_ratio(normalized[i], normalized[j]) >= FUZZY_THRESHOLD:
                union(i, j)

    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[find(i)].append(row)

    return [g for g in groups.values() if len(g) > 1]


def _merge_group(group, db, apply: bool) -> dict:
    """
    Merge one duplicate group in-memory using the exact same field-fill
    logic live ingestion uses. If apply=False (dry run) the merge is rolled
    back after computing the preview — nothing is written. If apply=True,
    the merge is committed and losers are deleted from PG + Qdrant.
    """
    from processing.storage import _fill_empty_fields
    from sqlalchemy.orm.attributes import flag_modified

    group_sorted = sorted(group, key=lambda r: r.created_at or datetime.min)
    keeper = group_sorted[0]
    losers = group_sorted[1:]

    before = {c.name: getattr(keeper, c.name) for c in keeper.__table__.columns}

    merged_history = list(keeper.source_history or [])
    known_urls = {e.get("url") for e in merged_history}

    for loser in losers:
        loser_dict = {
            "one_liner":      loser.short_description,  # _fill_empty_fields reads this key
            "description":    loser.description,
            "website":        loser.website,
            "industry":       loser.industry,
            "sub_industry":   loser.sub_industry,
            "tech_cluster":   loser.tech_cluster,
            "country":        loser.country,
            "city":           loser.city,
            "address":        loser.address,
            "funding_stage":  loser.funding_stage,
            "employee_count": loser.employee_count,
            "contact_info":   loser.contact_info,
            "linkedin":       loser.linkedin,
            "founded_year":   loser.founded_year,
            "tags":           loser.tags,
        }
        _fill_empty_fields(keeper, loser_dict)

        for entry in (loser.source_history or []):
            if entry.get("url") not in known_urls:
                merged_history.append(entry)
                known_urls.add(entry.get("url"))

    keeper.source_history = merged_history
    flag_modified(keeper, "source_history")

    after = {c.name: getattr(keeper, c.name) for c in keeper.__table__.columns}
    fields_filled = [f for f in before if before[f] != after[f] and f != "updated_at"]

    summary = {
        "keeper": keeper.name,
        "keeper_id": str(keeper.id),
        "losers": [(l.name, str(l.id)) for l in losers],
        "fields_filled": fields_filled,
    }

    if not apply:
        db.rollback()  # discard the in-memory merge — dry run writes nothing
        return summary

    keeper.updated_at = datetime.utcnow()

    from vector_db.qdrant_store import qdrant_store
    for loser in losers:
        try:
            qdrant_store.delete_startup(str(loser.id))
        except Exception as exc:
            logger.warning(f"     ! Qdrant delete failed for '{loser.name}' ({loser.id}): {exc}")
        db.delete(loser)

    db.commit()

    # Re-embed + re-upsert the keeper so Qdrant reflects the merged data
    from embeddings.embedder import embedder
    embed_text = embedder.build_startup_text({
        "name": keeper.name,
        "description": keeper.description,
        "industry": keeper.industry,
        "tags": keeper.tags,
    })
    vector = embedder.embed(embed_text)
    qdrant_store.upsert_startup(str(keeper.id), vector, {
        "id": str(keeper.id),
        "name": keeper.name,
        "description": keeper.description,
        "industry": keeper.industry,
        "country": keeper.country,
        "city": keeper.city,
        "funding_stage": keeper.funding_stage,
        "source": keeper.source,
        "source_url": keeper.source_url,
        "enrichment_score": keeper.enrichment_score or 0.0,
        "source_confidence": keeper.source_confidence or 0.0,
        "score_tier": keeper.score_tier or "WEAK_SIGNAL",
    })

    return summary


def run(apply: bool) -> None:
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    try:
        rows = db.query(Startup).all()
        logger.info(f"Scanning {len(rows)} startups for duplicates...\n")

        groups = _find_groups(rows)

        if not groups:
            logger.info("No duplicates found. Database is already clean.")
            return

        logger.info(f"Found {len(groups)} duplicate group(s):\n")

        total_losers = 0
        for i, group in enumerate(groups, 1):
            summary = _merge_group(group, db, apply=apply)
            total_losers += len(summary["losers"])
            logger.info(f"[{i}] KEEP:   '{summary['keeper']}' ({summary['keeper_id']})")
            for name, id_ in summary["losers"]:
                logger.info(f"     MERGE & DELETE: '{name}' ({id_})")
            if summary["fields_filled"]:
                logger.info(f"     Fields filled on keeper: {', '.join(summary['fields_filled'])}")
            logger.info("")

        if apply:
            logger.info(
                f"Done. Merged {total_losers} duplicate row(s) into {len(groups)} canonical record(s)."
            )
        else:
            logger.info(
                f"DRY RUN — no changes made. {total_losers} row(s) would be merged into "
                f"{len(groups)} canonical record(s). Re-run with --apply to execute for real."
            )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-time duplicate cleanup sweep")
    parser.add_argument("--apply", action="store_true", help="Execute the merge for real (default: dry run)")
    args = parser.parse_args()
    run(apply=args.apply)
