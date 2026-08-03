"""
Throughput ceiling benchmark — how fast can extraction actually go under
the GPU-mutex constraint (max_qwen_workers=1, one heavy LLM job at a time,
A.3)?

Crawls ONE real page (static fetch + real chunking, no LLM yet) to get a
realistic set of chunk texts, then repeatedly calls
qwen_client.extract_startups() directly (cycling through those chunks) to
measure the achieved calls/sec and the per-call latency distribution —
independent of crawl/network variance, which is what actually matters for
"how long would a full sweep take" questions (Phase T's premise).

Never writes to Postgres/Qdrant — extract_startups() is a pure LLM call,
nothing downstream of it runs.

Usage
-----
  python scripts/bench_throughput.py <url> [--n-calls 20] [--total-chunks N]

--total-chunks projects a full-sweep duration from the measured per-call
rate, given a known (or estimated) total chunk count across all sources —
e.g. the `chunks_created` sum from a recent /ingestion/status run history.
Omit it to just see the raw rate.

Exit code is always 0 — this is a diagnostic, not a gate.
"""
import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")


async def _get_sample_chunks(url: str, max_chunks: int) -> list:
    """Real fetch + real chunking, no LLM call — just gets representative text."""
    import httpx
    from ingestion.web_scraper import web_scraper, _extract_text
    from ingestion.chunker import split_web_page

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        html = await web_scraper._fetch_page(client, url)
    if not html:
        raise SystemExit(f"Could not fetch {url}")
    text = _extract_text(html)
    chunks = split_web_page(text)
    if not chunks:
        raise SystemExit(f"No chunks produced from {url} — page may be too thin/empty")
    return chunks[:max_chunks] if max_chunks else chunks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--n-calls", type=int, default=20, help="total extraction calls to run")
    ap.add_argument("--total-chunks", type=int, default=None,
                     help="known/estimated total chunk count across a full sweep, for a duration projection")
    args = ap.parse_args()

    print(f"Fetching + chunking {args.url} for sample text...")
    chunks = asyncio.run(_get_sample_chunks(args.url, max_chunks=max(1, args.n_calls // 2)))
    print(f"  {len(chunks)} distinct chunk(s) available, cycling to reach {args.n_calls} calls")

    from reasoning.qwen_client import qwen_client

    latencies = []
    failures = 0
    t_start = time.time()
    for i in range(args.n_calls):
        chunk = chunks[i % len(chunks)]
        t0 = time.time()
        try:
            qwen_client.extract_startups(chunk)
        except Exception as exc:
            failures += 1
            print(f"  call {i+1}/{args.n_calls} FAILED: {exc}")
            continue
        elapsed = time.time() - t0
        latencies.append(elapsed)
        print(f"  call {i+1}/{args.n_calls}: {elapsed:.1f}s")
    total_wall = time.time() - t_start

    print(f"\n{'='*60}")
    print(f"  THROUGHPUT — {args.url}")
    print(f"{'='*60}")
    print(f"  Calls attempted   : {args.n_calls}  ({failures} failed)")
    print(f"  Total wall time   : {total_wall:.1f}s")
    if latencies:
        print(f"  Achieved rate     : {len(latencies)/total_wall:.3f} calls/sec "
              f"({total_wall/len(latencies):.1f}s/call average)")
        print(f"  Latency min       : {min(latencies):.1f}s")
        print(f"  Latency median    : {statistics.median(latencies):.1f}s")
        if len(latencies) >= 5:
            print(f"  Latency p90       : {statistics.quantiles(latencies, n=10)[8]:.1f}s")
        print(f"  Latency max       : {max(latencies):.1f}s")

        avg = statistics.mean(latencies)
        print(f"\n  This is the GPU-mutex ceiling for THIS chunk profile: with")
        print(f"  max_qwen_workers=1, no amount of pipeline concurrency elsewhere")
        print(f"  (crawling, chunking, storage) makes extraction itself faster —")
        print(f"  only fewer/cheaper calls (Phase T's premise) does.")

        if args.total_chunks:
            projected = args.total_chunks * avg
            print(f"\n  Projected full-sweep extraction time at {args.total_chunks} total chunks:")
            print(f"    {projected:.0f}s  (~{projected/60:.1f} min)")
    else:
        print("  All calls failed — no latency data.")


if __name__ == "__main__":
    main()
