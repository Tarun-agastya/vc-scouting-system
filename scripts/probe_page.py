"""
Pipeline accuracy probe (diagnostic, 23 Jul) — answers two questions the
metrics dashboard can't, both about whether we're actually SEEING the
startups a source contains:

  COVERAGE  — "does the crawler move forward, or re-crawl the same handful of
              pages?" and "does it click INTO individual startup detail pages,
              or only read the listing?" Runs the REAL deep crawler with the
              REAL production settings (max_depth=2, max_pages=10 — the
              defaults every scout_controller run uses) and prints the exact
              list of URLs it visits, in visit order, with depth. If that list
              is 10 nav/listing pages and zero /startups/<company> detail
              pages, coverage is the problem, not extraction.

  RECALL    — "on ONE page that lists N startups, how many of the N do we
              actually extract?" Fetches a single page (with the same
              Playwright fallback the crawler uses, so JS-rendered sites work),
              chunks + filters it exactly like the pipeline, runs the real 7B
              extraction on each surviving chunk, and prints the deduplicated
              list of startup NAMES we got. You open the same page, count the
              startups by hand, and compare — that human count is the ground
              truth this tool is meant to be measured against.

Usage
-----
    python scripts/probe_page.py coverage <url>          # which pages a real crawl reaches
    python scripts/probe_page.py coverage <url> --max-pages 30 --max-depth 3
    python scripts/probe_page.py recall <url>            # how many startups we extract from ONE page

Note on GPU contention: recall mode makes real extraction calls to Ollama. If
a verification/ingestion run is in flight, this queues behind it on the single
GPU (the mutex only governs in-process callers; this standalone script shares
the same Ollama). It won't crash — it may just be slow. For a clean recall
number, run it during an idle window.
"""
import sys
import os
import argparse
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def coverage(url: str, source_type: str, max_depth: int, max_pages: int) -> None:
    """Run the real crawler and report every URL it actually visits."""
    import httpx
    from ingestion.web_scraper import web_scraper
    from ingestion.worker_queue import PipelineMetrics, PageItem

    page_queue: asyncio.Queue = asyncio.Queue()
    metrics = PipelineMetrics()

    # Run the REAL BFS crawler task, then drain the pages it emitted.
    await web_scraper._crawler_task(
        url, source_type, max_depth, max_pages, page_queue, metrics
    )

    visited = []
    while not page_queue.empty():
        item = await page_queue.get()
        if item is None:
            continue
        visited.append(item.url)

    print(f"\n{'='*74}")
    print(f"COVERAGE PROBE  —  {url}")
    print(f"settings: max_pages={max_pages}, max_depth={max_depth}  "
          f"(priority frontier: startup/portfolio pages visited first)")
    print(f"{'='*74}")
    print(f"pages fetched with usable text : {len(visited)}")
    print(f"pages skipped (empty/failed)   : {metrics.pages_skipped}")
    print(f"\nURLs visited, in crawl order:")
    for i, u in enumerate(visited, 1):
        print(f"  {i:2}. {u}")

    # How many look like individual company/detail pages vs listing/nav pages?
    detail_markers = ("/startup", "/startups/", "/company", "/companies/",
                      "/portfolio/", "/unternehmen", "/member")
    detail = [u for u in visited if any(m in u.lower() for m in detail_markers)]
    print(f"\nof those, {len(detail)} look like individual detail pages "
          f"(URL contains /startup, /company, /portfolio, …):")
    for u in detail:
        print(f"     · {u}")
    if not detail:
        print("     (none — this crawl only saw listing/section pages, "
              "not individual startup profiles)")
    print()


async def recall(url: str, force_render: bool = False) -> None:
    """Fetch ONE page and report exactly which startups we extract from it."""
    import httpx
    from ingestion.web_scraper import web_scraper, _extract_text, _HEADERS
    from ingestion.chunker import split
    from ingestion.candidate_filter import is_relevant
    from reasoning.qwen_client import qwen_client

    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=True, max_redirects=5,
    ) as client:
        html = await web_scraper._fetch_page(client, url, force_render=force_render)

    text = _extract_text(html) if html else ""
    print(f"\n{'='*74}")
    print(f"RECALL PROBE  —  {url}")
    print(f"{'='*74}")
    print(f"page text extracted   : {len(text):,} chars")
    if len(text) < 200:
        print("  ⚠ almost no text — this page is empty to us. If it clearly "
              "has content in a browser, it's JS-rendered and the fetch "
              "(even with Playwright fallback) didn't get it.")
        return

    chunks = split(text)
    relevant = [c for c in chunks if is_relevant(c)]
    print(f"chunks produced       : {len(chunks)}")
    print(f"chunks kept by filter : {len(relevant)}  "
          f"({len(chunks) - len(relevant)} dropped as boilerplate)")
    print(f"\nrunning extraction on {len(relevant)} chunk(s) "
          f"(real 7B model — may queue behind other GPU work)…\n")

    all_names = []
    seen = set()
    for i, chunk in enumerate(relevant, 1):
        try:
            startups = await asyncio.get_event_loop().run_in_executor(
                None, qwen_client.extract_startups, chunk
            )
        except Exception as exc:
            print(f"  chunk {i}/{len(relevant)}: extraction FAILED ({exc})")
            continue
        names = [s.get("name", "").strip() for s in startups if s.get("name")]
        print(f"  chunk {i}/{len(relevant)}: {len(names)} startup(s) → "
              f"{', '.join(names) if names else '(none)'}")
        for n in names:
            key = n.lower()
            if key not in seen:
                seen.add(key)
                all_names.append(n)

    print(f"\n{'-'*74}")
    print(f"TOTAL DISTINCT STARTUPS WE EXTRACTED FROM THIS PAGE: {len(all_names)}")
    print(f"{'-'*74}")
    for i, n in enumerate(sorted(all_names, key=str.lower), 1):
        print(f"  {i:2}. {n}")
    print(f"\n→ Now open {url} in a browser, count the startups listed by hand,")
    print(f"  and compare that number to {len(all_names)}. That gap is our recall loss.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline coverage + recall probe")
    parser.add_argument("mode", choices=["coverage", "recall"])
    parser.add_argument("url")
    parser.add_argument("--source-type", default="general")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--render", action="store_true",
                        help="force headless-browser rendering (mirrors a source's render_mode: always)")
    args = parser.parse_args()

    if args.mode == "coverage":
        asyncio.run(coverage(args.url, args.source_type, args.max_depth, args.max_pages))
    else:
        asyncio.run(recall(args.url, force_render=args.render))


if __name__ == "__main__":
    main()
