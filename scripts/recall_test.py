"""
Adaptive-pipeline recall test — one page per source, DRY RUN.

Measures, per source, how many entities the page structurally offers vs how
many the pipeline actually extracts from it. This is the Phase R-7
generalization question ("does inspect-then-extract hold up across site
shapes it wasn't tuned on?") asked across the whole registry instead of the
four sources R-7 covered.

Dry run
-------
`processing.storage.upsert_startup` is monkeypatched to a counting stub, so
real fetch/chunk/Qwen-extraction happens but nothing reaches production
Postgres/Qdrant — the same method used for the R-4/R-7 verifications. Note
that SiteProfile writes are still real: that table is an ever-growing
learned cache by design, and a run may add/refresh a profile.

Recall definition
-----------------
    recall = entities_extracted_distinct / entities_expected

`entities_expected` is recomputed live at fetch time by the structural
inspector on every run — never read from the SiteProfile cache. That is a
deliberate safety property (a cached count cannot notice a site redesign),
so this test measures against what the page structurally looks like TODAY.

The profile cache IS consulted for one thing only: choosing WHICH url to
test. A source's registered primary_url is usually its homepage, which
profiles as `non_content` with a handful of nav links — measuring recall
there says nothing. So each source is tested on the highest-entity-count
page already known for its domain, falling back to primary_url when no
listing page is known.

Pages that offer no structural expectation (expected == 0 — prose/article
shapes) have NO defined recall and are reported separately rather than
counted as 0%, mirroring PipelineMetrics.shortfall_pages()'s own rule that
absence of an expectation is not evidence of a shortfall.

Usage
-----
    python scripts/recall_test.py                      # every testable source
    python scripts/recall_test.py --source-id zollhof  # just one
    python scripts/recall_test.py --json out.json      # machine-readable too
"""
import argparse
import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Sources that cannot yield a meaningful measurement: login/paywalled
# intelligence platforms, and the placeholder registry entry.
_SKIP_SOURCE_IDS = {"linkedin", "crunchbase", "tracxn", "new_accelerator"}

# Below this, a profile's "expected" is nav chrome rather than a real entity
# listing (homepages consistently profile at 4-5), so it isn't worth
# preferring over the registered primary_url.
_MIN_USEFUL_EXPECTED = 6


def _load_sources(source_id: str = None) -> list:
    import yaml
    with open("config/sources.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for s in data.get("web_sources") or []:
        sid = s.get("source_id")
        if source_id and sid != source_id:
            continue
        if not source_id and sid in _SKIP_SOURCE_IDS:
            continue
        if s.get("primary_url"):
            out.append(s)
    return out


def _best_url_for(source: dict) -> tuple:
    """(url, why) — the known listing page for this domain, else primary_url."""
    from database.connection import SessionLocal
    from database.models import SiteProfile
    from processing.site_profile_store import normalize_domain

    primary = source["primary_url"]
    domain = normalize_domain(primary)
    db = SessionLocal()
    try:
        rows = (
            db.query(SiteProfile)
            .filter(SiteProfile.domain == domain)
            .filter(SiteProfile.expected_entity_count >= _MIN_USEFUL_EXPECTED)
            .order_by(SiteProfile.expected_entity_count.desc())
            .all()
        )
        for r in rows:
            pattern = (r.url_pattern or "").rstrip("/")
            # A wildcard pattern ("/startups/*") names a shape, not a page —
            # only a concrete path can be fetched directly.
            if not pattern or "*" in pattern:
                continue
            return f"https://{domain}{pattern}", f"known listing page (profile expects ~{r.expected_entity_count})"
        return primary, "registered primary_url (no known listing page for this domain)"
    finally:
        db.close()


async def _measure(url: str, source_type: str) -> dict:
    """One-page dry-run extraction. Returns the run's metrics."""
    import processing.storage as storage_mod
    from ingestion.web_scraper import WebScraper
    from ingestion.worker_queue import PipelineMetrics

    real_upsert = storage_mod.upsert_startup
    stored = []

    def _stub(startup, source, source_url, published_date=None, provenance=None, origin_url=None):
        stored.append((startup.get("name") or "").strip())
        return None, "new_master"

    storage_mod.upsert_startup = _stub
    metrics = PipelineMetrics()
    t0 = time.time()
    try:
        await WebScraper().scrape_source(
            url, source_type, max_depth=0, max_pages=1, metrics=metrics,
        )
    finally:
        storage_mod.upsert_startup = real_upsert

    per_page = list(getattr(metrics, "per_page", {}).values())
    shape = next((p.page_shape for p in per_page if getattr(p, "page_shape", "")), "")
    return {
        "expected": metrics.entities_expected,
        "extracted_distinct": metrics.entities_extracted_distinct,
        "stored_names": sorted({n for n in stored if n}),
        "qwen_calls": metrics.qwen_calls,
        "qwen_failures": metrics.qwen_failures,
        "chunks_created": metrics.chunks_created,
        "pages_crawled": metrics.pages_crawled,
        "page_shape": shape,
        "elapsed_s": round(time.time() - t0, 1),
    }


async def main_async(source_id: str, json_out: str) -> None:
    from config import settings

    sources = _load_sources(source_id)
    if not sources:
        print(f"No matching source{f' {source_id!r}' if source_id else ''}.")
        return

    print(f"Adaptive pipeline: {'ON' if settings.adaptive_pipeline_enabled else 'OFF'}")
    print(f"Testing {len(sources)} source(s), one page each, dry run.\n")

    results = []
    for i, s in enumerate(sources, 1):
        sid = s.get("source_id")
        url, why = _best_url_for(s)
        print(f"[{i}/{len(sources)}] {sid} — {url}\n    ({why})", flush=True)
        try:
            m = await _measure(url, s.get("source_type", "general"))
            m["error"] = None
        except Exception as exc:
            m = {"expected": 0, "extracted_distinct": 0, "stored_names": [], "qwen_calls": 0,
                 "qwen_failures": 0, "chunks_created": 0, "pages_crawled": 0,
                 "page_shape": "", "elapsed_s": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        m.update({"source_id": sid, "url": url, "url_choice": why,
                  "source_type": s.get("source_type")})
        results.append(m)

        if m["error"]:
            print(f"    ERROR {m['error']}\n", flush=True)
        else:
            rec = (f"{m['extracted_distinct'] / m['expected'] * 100:.0f}%"
                   if m["expected"] else "n/a (no structural expectation)")
            print(f"    shape={m['page_shape'] or '?'} expected={m['expected']} "
                  f"extracted={m['extracted_distinct']} recall={rec} "
                  f"({m['qwen_calls']} calls, {m['elapsed_s']}s)\n", flush=True)

    _report(results)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nJSON written to {json_out}")


def _report(results: list) -> None:
    ok = [r for r in results if not r["error"]]
    measurable = [r for r in ok if r["expected"] > 0]
    # A page that was never fetched, or was fetched and then dropped before
    # reaching the extractor, is NOT a "prose page with no expectation" —
    # conflating the two hides real failures behind a benign-looking label.
    unreachable = [r for r in ok if r["expected"] == 0 and r["pages_crawled"] == 0]
    no_expect = [r for r in ok if r["expected"] == 0 and r["pages_crawled"] > 0]
    errored = [r for r in results if r["error"]]

    print("=" * 78)
    print("PER-SOURCE RECALL (pages with a structural expectation)")
    print("=" * 78)
    if measurable:
        print(f"{'source':22s} {'shape':16s} {'exp':>5s} {'got':>5s} {'recall':>8s}")
        for r in sorted(measurable, key=lambda x: -(x["extracted_distinct"] / max(1, x["expected"]))):
            pct = r["extracted_distinct"] / r["expected"] * 100
            print(f"{r['source_id']:22s} {r['page_shape'][:16]:16s} "
                  f"{r['expected']:5d} {r['extracted_distinct']:5d} {pct:7.0f}%")
        te, tg = sum(r["expected"] for r in measurable), sum(r["extracted_distinct"] for r in measurable)
        print("-" * 78)
        print(f"{'OVERALL':22s} {'':16s} {te:5d} {tg:5d} {tg / te * 100:7.0f}%")
        print(f"\nOverall recall is entity-weighted (total extracted / total expected),")
        print(f"so large listings count proportionally. Unweighted mean of the")
        print(f"per-source rates: {sum(r['extracted_distinct'] / r['expected'] for r in measurable) / len(measurable) * 100:.0f}%")
    else:
        print("  none")

    if unreachable:
        print("\n" + "=" * 78)
        print("NOT MEASURED — page never reached the extractor")
        print("=" * 78)
        print("These are failures, not prose pages: nothing was crawled, so there was")
        print("never anything to extract. Cause is upstream of the pipeline's recall.")
        for r in unreachable:
            print(f"  {r['source_id']:22s} {r['url']}")

    if no_expect:
        print("\n" + "=" * 78)
        print("NO STRUCTURAL EXPECTATION (prose/article pages — recall undefined)")
        print("=" * 78)
        print("Fetched and extracted, but the page offers no repeating entity grid to")
        print("measure against, so a percentage would be meaningless.")
        for r in no_expect:
            print(f"  {r['source_id']:22s} {r['page_shape'][:16] or '?':16s} "
                  f"extracted={r['extracted_distinct']:3d}  {r['url']}")

    if errored:
        print("\n" + "=" * 78)
        print("FAILED")
        print("=" * 78)
        for r in errored:
            print(f"  {r['source_id']:22s} {r['error']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", default=None, help="Test only this source")
    ap.add_argument("--json", dest="json_out", default=None, help="Also write results as JSON")
    args = ap.parse_args()
    asyncio.run(main_async(args.source_id, args.json_out))


if __name__ == "__main__":
    main()
