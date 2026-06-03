"""
Human Validation Mode — CLI entry point.

Three-stage workflow
--------------------
  capture  Scrape one source and write validation/{run_id}/capture.jsonl
  export   Convert capture.jsonl → validation/{run_id}/export.csv
  metrics  Read reviewed.csv → print metrics + write metrics.json

Usage
-----
  # Stage 1 — scrape and capture extraction provenance
  python scripts/run_validation.py capture \\
      --source-url https://www.munich-startup.de/startups/ \\
      --source-type startup_network \\
      --max-pages 10

  # Stage 2 — export to reviewable CSV
  python scripts/run_validation.py export --run-id <uuid>

  # (human opens export.csv, fills 'verdict' + 'reviewer_notes', saves as reviewed.csv)

  # Stage 3 — compute metrics from reviewed CSV
  python scripts/run_validation.py metrics --run-id <uuid>

Valid verdict values (reviewer fills these):
  VALID    — company is real, name correct, belongs in DACH startup index
  INVALID  — hallucination, wrong entity type, or confirmed duplicate
  PARTIAL  — company real but name truncated / misspelled / incomplete

Available source types:
  accelerator | incubator | university_hub | startup_network |
  intelligence_platform | newsletter | rss | general
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ── Stage handlers ────────────────────────────────────────────────────────────

def cmd_capture(args: argparse.Namespace) -> None:
    from ingestion.web_scraper import web_scraper
    from validation.capture import ValidationSession

    with ValidationSession(source_url=args.source_url) as vsession:
        print()
        print(f"  Run ID      : {vsession.run_id}")
        print(f"  Source URL  : {args.source_url}")
        print(f"  Source type : {args.source_type}")
        print(f"  Max pages   : {args.max_pages}")
        print()

        asyncio.run(
            web_scraper.scrape_source(
                url=args.source_url,
                source_type=args.source_type,
                max_pages=args.max_pages,
                validation_session=vsession,
            )
        )

    print()
    print(f"  Capture complete.")
    print(f"  Artifacts : validation/{vsession.run_id}/capture.jsonl")
    print()
    print(
        f"  Next:\n"
        f"    python scripts/run_validation.py export --run-id {vsession.run_id}"
    )
    print()


def cmd_export(args: argparse.Namespace) -> None:
    from validation.exporter import export_to_csv

    csv_path = export_to_csv(args.run_id)
    print()
    print(f"  Exported  → {csv_path}")
    print()
    print(
        "  Instructions:\n"
        "    1. Open export.csv in Excel / Google Sheets\n"
        "    2. Fill in the 'verdict' column for each row:\n"
        "         VALID   — correct extraction\n"
        "         INVALID — hallucination, wrong entity, or duplicate\n"
        "         PARTIAL — real company but name is wrong/truncated\n"
        "    3. Add optional notes in 'reviewer_notes'\n"
        f"   4. Save as  validation/{args.run_id}/reviewed.csv\n"
    )
    print(
        f"  Next:\n"
        f"    python scripts/run_validation.py metrics --run-id {args.run_id}"
    )
    print()


def cmd_metrics(args: argparse.Namespace) -> None:
    from validation.metrics import compute_metrics

    report = compute_metrics(args.run_id)
    print(
        f"  Metrics written → validation/{args.run_id}/metrics.json"
    )


# ── CLI parser ────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_validation.py",
        description="VC Scouting — Human Validation Mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    # ── capture ───────────────────────────────────────────────────────────────
    capture_p = sub.add_parser(
        "capture",
        help="Scrape one source URL and capture extraction provenance",
    )
    capture_p.add_argument(
        "--source-url",
        required=True,
        metavar="URL",
        help="Top-level URL to scrape (e.g. https://www.xpreneurs.io/portfolio)",
    )
    capture_p.add_argument(
        "--source-type",
        default="general",
        metavar="TYPE",
        help=(
            "Source type for scoring weight. "
            "One of: accelerator | incubator | university_hub | "
            "startup_network | intelligence_platform | newsletter | rss | general. "
            "Default: general"
        ),
    )
    capture_p.add_argument(
        "--max-pages",
        type=int,
        default=10,
        metavar="N",
        help="Maximum pages to crawl. Default: 10",
    )
    capture_p.set_defaults(func=cmd_capture)

    # ── export ────────────────────────────────────────────────────────────────
    export_p = sub.add_parser(
        "export",
        help="Convert capture.jsonl to a human-reviewable CSV",
    )
    export_p.add_argument(
        "--run-id",
        required=True,
        metavar="UUID",
        help="Run ID returned by the capture stage",
    )
    export_p.set_defaults(func=cmd_export)

    # ── metrics ───────────────────────────────────────────────────────────────
    metrics_p = sub.add_parser(
        "metrics",
        help="Compute quality metrics from reviewed.csv",
    )
    metrics_p.add_argument(
        "--run-id",
        required=True,
        metavar="UUID",
        help="Run ID returned by the capture stage",
    )
    metrics_p.set_defaults(func=cmd_metrics)

    return parser


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    print()
    print("=" * 64)
    print(f"  VC Scouting — Human Validation Mode  [{args.stage}]")
    print("=" * 64)

    args.func(args)


if __name__ == "__main__":
    main()
