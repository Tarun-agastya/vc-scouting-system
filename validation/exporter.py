"""
Export a capture.jsonl to a human-reviewable flat CSV.

The exported CSV has two empty reviewer columns appended:
  verdict         — VALID | INVALID | PARTIAL
  reviewer_notes  — free text

Usage
-----
  from validation.exporter import export_to_csv
  csv_path = export_to_csv(run_id)

Or via CLI:
  python scripts/run_validation.py export --run-id <uuid>
"""

import csv
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "validation"

# Column order in the exported CSV
_EXPORT_FIELDS = [
    "run_id",
    "source_url",
    "page_url",
    "chunk_id",
    "chunk_preview",
    "extracted_company_name",
    "stored",
    "record_id",
    "qwen_duration_s",
    "captured_at",
    # ── reviewer fills these two columns ──────────────────────────────────────
    "verdict",
    "reviewer_notes",
]


def export_to_csv(run_id: str) -> Path:
    """
    Read ``validation/{run_id}/capture.jsonl`` and write
    ``validation/{run_id}/export.csv``.

    Rows are sorted by (page_url, chunk_id) so the reviewer reads the source
    page-by-page rather than in capture order.

    Returns the path to the exported CSV.
    Raises FileNotFoundError if the capture file does not exist.
    Raises ValueError if the capture file is empty.
    """
    run_dir      = _VALIDATION_DIR / run_id
    capture_path = run_dir / "capture.jsonl"

    if not capture_path.exists():
        raise FileNotFoundError(
            f"Capture file not found: {capture_path}\n"
            "Run the capture stage first:\n"
            f"  python scripts/run_validation.py capture --source-url <url>"
        )

    records = []
    with open(capture_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if not records:
        raise ValueError(f"Capture file is empty: {capture_path}")

    # Sort for ergonomic review
    records.sort(key=lambda r: (r.get("page_url", ""), r.get("chunk_id", "")))

    csv_path = run_dir / "export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        # utf-8-sig so Excel auto-detects UTF-8 with the BOM signature
        writer = csv.DictWriter(
            fh, fieldnames=_EXPORT_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for rec in records:
            row = {field: rec.get(field, "") for field in _EXPORT_FIELDS}
            row["verdict"]        = ""
            row["reviewer_notes"] = ""
            writer.writerow(row)

    logger.info(
        f"[Validation] Exported {len(records)} record(s) → {csv_path}"
    )
    return csv_path
