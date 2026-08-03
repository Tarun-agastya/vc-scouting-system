"""
Pipeline stage-timing benchmark — real fetch/chunk/extract, DRY RUN by
default (never writes to Postgres/Qdrant).

Runs a real web_scraper.scrape_source() against a live URL — same code path
as production ingestion — and reports where wall-clock time actually goes:
fetch/render, chunking+filtering, Qwen extraction, storage. Answers "which
Phase T throughput hypothesis actually matters for THIS source" with
measured numbers instead of guesses.

Also supports an adaptive on/off A/B comparison in one run (recall + speed
side by side) — the promoted, reusable version of the ad-hoc dry-run script
used to live-verify Phase R-4 (see validation/R4_VERIFICATION.md).

Usage
-----
  python scripts/bench_pipeline.py <url> [--max-pages N]
  python scripts/bench_pipeline.py <url> --adaptive both   # A/B comparison
  python scripts/bench_pipeline.py <url> --live            # DANGEROUS: real DB/Qdrant writes

Exit code is always 0 — this is a diagnostic, not a gate.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")


def _install_stub():
    """Dry-run mode: count what WOULD be stored without touching PG/Qdrant."""
    stored = []

    def fake_upsert(startup, source, source_url, published_date=None, provenance=None, origin_url=None):
        name = (startup.get("name") or "").strip()
        if not name or len(name) < 2:
            return None, None
        stored.append(name)
        return "bench-dry-run-id", "new_master"

    import processing.storage as storage_mod
    import ingestion.worker_queue as wq_mod
    storage_mod.upsert_startup = fake_upsert
    wq_mod.upsert_startup = fake_upsert
    return stored


async def _run(url: str, *, adaptive: bool, max_pages: int) -> dict:
    from ingestion.web_scraper import web_scraper

    os.environ["ADAPTIVE_PIPELINE_ENABLED"] = "true" if adaptive else "false"
    # config.settings is a module-level singleton read once at import — for
    # a same-process A/B comparison we must force it to re-read the env var
    # each time rather than relying on process-start-time caching.
    from config import settings
    settings.adaptive_pipeline_enabled = adaptive

    stored = _install_stub()
    t0 = time.time()
    metrics = await web_scraper.scrape_source(url=url, source_type="bench", max_pages=max_pages)
    wall = time.time() - t0

    return {
        "adaptive": adaptive,
        "wall_s": round(wall, 1),
        "pages_crawled": metrics.pages_crawled,
        "pages_rendered": metrics.pages_rendered,
        "pages_static": metrics.pages_static,
        "chunks_created": metrics.chunks_created,
        "chunks_filtered": metrics.chunks_filtered,
        "chunks_bypassed_filter": metrics.chunks_bypassed_filter,
        "qwen_calls": metrics.qwen_calls,
        "qwen_failures": metrics.qwen_failures,
        "fetch_time_s": round(metrics.fetch_time_s, 1),
        "chunk_time_s": round(metrics.chunk_time_s, 1),
        "qwen_time_s": round(metrics.qwen_time_s, 1),
        "storage_time_s": round(metrics.storage_time_s, 1),
        "distinct_names": len(set(n.strip().lower() for n in stored if n.strip())),
        "raw_records": len(stored),
        "names": sorted(set(stored)),
    }


def _print_report(r: dict) -> None:
    wall = r["wall_s"] or 0.001
    print(f"\n{'='*70}")
    print(f"  adaptive_pipeline_enabled = {r['adaptive']}")
    print(f"{'='*70}")
    print(f"  Wall clock       : {r['wall_s']}s")
    print(f"  Pages crawled    : {r['pages_crawled']}  (rendered={r['pages_rendered']} static={r['pages_static']})")
    print(f"  Chunks           : {r['chunks_created']} created, {r['chunks_filtered']} filtered, "
          f"{r['chunks_bypassed_filter']} bypassed-filter")
    print(f"  Qwen calls       : {r['qwen_calls']} ({r['qwen_failures']} failed)")
    print(f"  Extracted        : {r['distinct_names']} distinct names ({r['raw_records']} raw records)")
    print(f"\n  Stage breakdown (cumulative work, stages overlap so this can exceed wall clock):")
    for stage in ("fetch_time_s", "chunk_time_s", "qwen_time_s", "storage_time_s"):
        secs = r[stage]
        pct = round(secs / wall * 100) if wall else 0
        bar = "#" * min(50, round(pct / 2))
        print(f"    {stage:16s} {secs:7.1f}s  ({pct:4d}% of wall)  {bar}")
    if r["names"]:
        print(f"\n  Names ({len(r['names'])}):")
        for n in r["names"]:
            print(f"    - {n}")


def _print_comparison(off: dict, on: dict) -> None:
    print(f"\n{'='*70}")
    print(f"  A/B COMPARISON — adaptive off vs on")
    print(f"{'='*70}")
    rows = [
        ("wall_s", "Wall clock (s)"),
        ("qwen_calls", "Qwen calls"),
        ("distinct_names", "Distinct names"),
        ("fetch_time_s", "Fetch time (s)"),
        ("chunk_time_s", "Chunk time (s)"),
        ("qwen_time_s", "Qwen time (s)"),
    ]
    print(f"  {'':20s} {'OFF':>10s} {'ON':>10s} {'delta':>10s}")
    for key, label in rows:
        o, n = off[key], on[key]
        delta = round(n - o, 1) if isinstance(o, float) else n - o
        sign = "+" if delta >= 0 else ""
        print(f"  {label:20s} {o:>10} {n:>10} {sign}{delta:>9}")

    off_names, on_names = set(n.lower() for n in off["names"]), set(n.lower() for n in on["names"])
    lost = sorted(off_names - on_names)
    gained = sorted(on_names - off_names)
    print(f"\n  Names in OFF but not ON ({len(lost)}): {lost}")
    print(f"  Names in ON but not OFF ({len(gained)}): {gained}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--adaptive", choices=["off", "on", "both"], default="off")
    ap.add_argument("--live", action="store_true", help="DANGEROUS: real DB/Qdrant writes, no stub")
    args = ap.parse_args()

    if args.live:
        print("!! --live: writing to real Postgres/Qdrant. Ctrl-C now to abort.")

    async def run_all():
        global _install_stub
        if args.live:
            _install_stub_orig = _install_stub
            globals()["_install_stub"] = lambda: []  # no-op: don't stub, let real writes happen

        if args.adaptive == "both":
            off = await _run(args.url, adaptive=False, max_pages=args.max_pages)
            _print_report(off)
            on = await _run(args.url, adaptive=True, max_pages=args.max_pages)
            _print_report(on)
            _print_comparison(off, on)
        else:
            r = await _run(args.url, adaptive=(args.adaptive == "on"), max_pages=args.max_pages)
            _print_report(r)

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
