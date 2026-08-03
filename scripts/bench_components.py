"""
Component micro-benchmarks — isolated timing of the pure/CPU-bound pieces of
the pipeline, on the SAME real HTML, so their relative cost is comparable.

Fetches one real page once, then times each of the following in isolation,
run --repeats times each (default 5) for a stable median (some of these have
first-call overhead — regex compilation, trafilatura's internal init):

  - _extract_text(html)                    legacy BS4 boilerplate strip + get_text
  - extract_content(..., "main_prose")     trafilatura path (Phase R-4)
  - site_inspector.probe_html(html, url)   structural card detection
  - chunker.split(text)                    sliding-window chunking
  - chunker.split_web_page(text)           the general web-page entry point
  - candidate_filter.is_relevant(chunk)    heuristic relevance gate, per chunk

None of these touch the network (after the one initial fetch) or Ollama —
this isolates CPU-bound cost, not I/O or LLM latency (see
scripts/bench_pipeline.py / bench_throughput.py for those).

Usage
-----
  python scripts/bench_components.py <url> [--repeats 5]

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


def _time_it(fn, repeats: int) -> dict:
    times = []
    result = None
    for _ in range(repeats):
        t0 = time.time()
        result = fn()
        times.append(time.time() - t0)
    return {
        "median_ms": round(statistics.median(times) * 1000, 2),
        "min_ms": round(min(times) * 1000, 2),
        "max_ms": round(max(times) * 1000, 2),
        "result": result,
    }


async def _fetch(url: str) -> str:
    import httpx
    from ingestion.web_scraper import web_scraper
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        return await web_scraper._fetch_page(client, url)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()

    print(f"Fetching {args.url} once...")
    html = asyncio.run(_fetch(args.url))
    if not html:
        raise SystemExit("Fetch failed — nothing to benchmark")
    print(f"  {len(html):,} bytes of HTML fetched\n")

    from ingestion.web_scraper import _extract_text, extract_content
    from ingestion.strategy import PageStrategy
    from ingestion.site_inspector import probe_html
    from ingestion.chunker import split, split_web_page
    from ingestion.candidate_filter import is_relevant

    results = {}

    results["_extract_text (BS4 legacy)"] = _time_it(lambda: _extract_text(html), args.repeats)

    prose_strat = PageStrategy(text_extraction="main_prose")
    results["extract_content main_prose (trafilatura)"] = _time_it(
        lambda: extract_content(html, args.url, prose_strat), args.repeats
    )

    results["site_inspector.probe_html (structural detection)"] = _time_it(
        lambda: probe_html(html, args.url), args.repeats
    )

    text = _extract_text(html)
    results["chunker.split (sliding window)"] = _time_it(lambda: split(text), args.repeats)
    results["chunker.split_web_page (general entry point)"] = _time_it(
        lambda: split_web_page(text), args.repeats
    )

    chunks = split(text) or [text[:1800]]
    sample_chunk = chunks[0]
    results["candidate_filter.is_relevant (per chunk)"] = _time_it(
        lambda: is_relevant(sample_chunk), args.repeats
    )

    print(f"{'='*78}")
    print(f"  COMPONENT TIMINGS — {args.repeats} repeats each, median/min/max")
    print(f"{'='*78}")
    for name, r in results.items():
        print(f"  {name:48s} {r['median_ms']:8.2f}ms  (min {r['min_ms']:.2f} / max {r['max_ms']:.2f})")

    prose_len = len(results["extract_content main_prose (trafilatura)"]["result"].text)
    bs4_len = len(results["_extract_text (BS4 legacy)"]["result"])
    print(f"\n  Output size — BS4: {bs4_len:,} chars, trafilatura: {prose_len:,} chars "
          f"({round((1 - prose_len/bs4_len)*100) if bs4_len else 0}% reduction)")

    sig = results["site_inspector.probe_html (structural detection)"]["result"]
    if sig.primary_group:
        print(f"  Structural inspector found a primary group: {sig.primary_group.n} items, "
              f"score {sig.primary_group.score:.2f}")
    else:
        print(f"  Structural inspector found no card group above threshold on this page.")

    print(f"\n  All of these are CPU-bound and typically sub-100ms — for context, a single")
    print(f"  Qwen extraction call on this pipeline runs 3-35s (see bench_throughput.py).")
    print(f"  Component cost is very unlikely to be the bottleneck; this benchmark exists")
    print(f"  to prove that quantitatively rather than assume it.")


if __name__ == "__main__":
    main()
