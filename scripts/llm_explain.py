"""
Manual trigger for the async LLM review explainer (Phase S-3b, Layer 4).

Normally this runs automatically as a nightly scheduler job inside the API
process (api/main.py), where it shares the GPU mutex with ingestion. Use this
CLI only for a manual off-hours run when the pipeline is idle — running it
concurrently with a heavy ingestion sweep could contend for the GPU.

Usage:
    python scripts/llm_explain.py [--limit N]
"""
import sys
import os
import asyncio
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from processing.review_explainer import explain_pending_reviews


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explain pending duplicate reviews with the local LLM")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    count = asyncio.run(explain_pending_reviews(limit=args.limit))
    print(f"Explained {count} pending review(s).")
