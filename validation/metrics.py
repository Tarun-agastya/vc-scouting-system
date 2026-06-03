"""
Compute quality metrics from a human-reviewed validation CSV.

Reads ``validation/{run_id}/reviewed.csv`` (falls back to export.csv so
the command is usable even before a human pass, showing 0 verdicts).

Writes ``validation/{run_id}/metrics.json`` and prints a formatted summary.

Metrics
-------
  precision           = VALID / (VALID + INVALID)          (excludes PARTIAL)
  false_positive_rate = INVALID / reviewed_rows
  partial_rate        = PARTIAL / reviewed_rows
  extraction_rate     = rows_with_a_name / total_rows
  coverage_gap_count  = (none) rows marked INVALID by reviewer
                        (chunks that clearly contained a startup but Qwen missed)
  startups_per_page   = per-page breakdown of valid/invalid/partial/stored counts

Usage
-----
  from validation.metrics import compute_metrics
  report = compute_metrics(run_id)

Or via CLI:
  python scripts/run_validation.py metrics --run-id <uuid>
"""

import csv
import dataclasses
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_VALIDATION_DIR  = Path(__file__).resolve().parent.parent / "validation"
_VALID_VERDICTS  = {"VALID", "INVALID", "PARTIAL"}


# ── Report dataclass ──────────────────────────────────────────────────────────

@dataclass
class MetricsReport:
    run_id:               str
    source_url:           str
    total_rows:           int
    reviewed_rows:        int
    unreviewed_count:     int
    valid_count:          int
    invalid_count:        int
    partial_count:        int
    none_extraction_count: int      # rows with extracted_company_name == "(none)"
    stored_count:         int
    coverage_gap_count:   int       # (none) rows marked INVALID
    precision:            Optional[float]
    false_positive_rate:  Optional[float]
    partial_rate:         Optional[float]
    extraction_rate:      Optional[float]
    avg_qwen_duration_s:  Optional[float]
    per_page_breakdown:   Dict[str, Dict]
    reviewer_notes_by_verdict: Dict[str, List[str]]


# ── Public API ────────────────────────────────────────────────────────────────

def compute_metrics(run_id: str) -> MetricsReport:
    """
    Load the reviewed CSV, compute all metrics, write metrics.json, and
    print a summary table to stdout.

    Returns a MetricsReport dataclass.
    """
    run_dir       = _VALIDATION_DIR / run_id
    reviewed_path = run_dir / "reviewed.csv"

    if not reviewed_path.exists():
        export_path = run_dir / "export.csv"
        if not export_path.exists():
            raise FileNotFoundError(
                f"Neither reviewed.csv nor export.csv found in {run_dir}.\n"
                "Run the export stage first:\n"
                f"  python scripts/run_validation.py export --run-id {run_id}"
            )
        reviewed_path = export_path
        logger.warning(
            "[Validation] reviewed.csv not found — computing stats on export.csv "
            "(verdict columns will be empty; all rows counted as unreviewed)"
        )

    rows = []
    with open(reviewed_path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)

    if not rows:
        raise ValueError(f"File is empty: {reviewed_path}")

    # ── Counters ──────────────────────────────────────────────────────────────
    total_rows      = len(rows)
    verdicts        = [r.get("verdict", "").strip().upper() for r in rows]

    reviewed_rows   = sum(1 for v in verdicts if v in _VALID_VERDICTS)
    valid_count     = verdicts.count("VALID")
    invalid_count   = verdicts.count("INVALID")
    partial_count   = verdicts.count("PARTIAL")
    unreviewed_count = total_rows - reviewed_rows

    none_extraction_count = sum(
        1 for r in rows if r.get("extracted_company_name", "") == "(none)"
    )
    stored_count = sum(
        1 for r in rows if r.get("stored", "").lower() in ("true", "1")
    )
    coverage_gap_count = sum(
        1 for r, v in zip(rows, verdicts)
        if r.get("extracted_company_name", "") == "(none)" and v == "INVALID"
    )

    # ── Qwen duration ─────────────────────────────────────────────────────────
    durations: List[float] = []
    for r in rows:
        try:
            durations.append(float(r.get("qwen_duration_s", 0) or 0))
        except ValueError:
            pass
    avg_qwen = round(sum(durations) / len(durations), 2) if durations else None

    # ── Derived rates ─────────────────────────────────────────────────────────
    real_denom      = valid_count + invalid_count
    precision       = round(valid_count / real_denom, 4)       if real_denom      else None
    fpr             = round(invalid_count / reviewed_rows, 4)  if reviewed_rows   else None
    partial_rate    = round(partial_count / reviewed_rows, 4)  if reviewed_rows   else None
    extraction_rows = sum(
        1 for r in rows if r.get("extracted_company_name", "") != "(none)"
    )
    extraction_rate = round(extraction_rows / total_rows, 4)   if total_rows      else None

    # ── Per-page breakdown ────────────────────────────────────────────────────
    per_page: Dict[str, Dict] = defaultdict(
        lambda: {"total": 0, "valid": 0, "invalid": 0, "partial": 0, "stored": 0}
    )
    for r, v in zip(rows, verdicts):
        page = r.get("page_url", "unknown")
        per_page[page]["total"] += 1
        if v == "VALID":
            per_page[page]["valid"] += 1
        elif v == "INVALID":
            per_page[page]["invalid"] += 1
        elif v == "PARTIAL":
            per_page[page]["partial"] += 1
        if r.get("stored", "").lower() in ("true", "1"):
            per_page[page]["stored"] += 1

    # ── Reviewer notes by verdict ─────────────────────────────────────────────
    notes_by_verdict: Dict[str, List[str]] = defaultdict(list)
    for r, v in zip(rows, verdicts):
        note = (r.get("reviewer_notes") or "").strip()
        if note and v in _VALID_VERDICTS:
            notes_by_verdict[v].append(note)

    source_url = rows[0].get("source_url", "unknown") if rows else "unknown"

    report = MetricsReport(
        run_id=run_id,
        source_url=source_url,
        total_rows=total_rows,
        reviewed_rows=reviewed_rows,
        unreviewed_count=unreviewed_count,
        valid_count=valid_count,
        invalid_count=invalid_count,
        partial_count=partial_count,
        none_extraction_count=none_extraction_count,
        stored_count=stored_count,
        coverage_gap_count=coverage_gap_count,
        precision=precision,
        false_positive_rate=fpr,
        partial_rate=partial_rate,
        extraction_rate=extraction_rate,
        avg_qwen_duration_s=avg_qwen,
        per_page_breakdown=dict(per_page),
        reviewer_notes_by_verdict=dict(notes_by_verdict),
    )

    # ── Persist ───────────────────────────────────────────────────────────────
    metrics_path = run_dir / "metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as fh:
        json.dump(dataclasses.asdict(report), fh, indent=2, ensure_ascii=False)
    logger.info(f"[Validation] Metrics written → {metrics_path}")

    _print_summary(report)
    return report


# ── Pretty-print ──────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    return f"{round(n / d * 100)}%" if d else "n/a"


def _fmt(v: Optional[float]) -> str:
    return str(v) if v is not None else "n/a"


def _print_summary(r: MetricsReport) -> None:
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  Validation Report  —  run {r.run_id[:8]}...")
    print(sep)
    print(f"  Source URL         : {r.source_url}")
    print(f"  Total rows         : {r.total_rows}")
    print(
        f"  Reviewed           : {r.reviewed_rows}  "
        f"({_pct(r.reviewed_rows, r.total_rows)})"
    )
    print(f"  Unreviewed         : {r.unreviewed_count}")
    print()
    print(f"  VALID              : {r.valid_count}")
    print(f"  INVALID            : {r.invalid_count}")
    print(f"  PARTIAL            : {r.partial_count}")
    print()
    print(f"  Precision          : {_fmt(r.precision)}")
    print(f"  False positive rate: {_fmt(r.false_positive_rate)}")
    print(f"  Partial rate       : {_fmt(r.partial_rate)}")
    print(f"  Extraction rate    : {_fmt(r.extraction_rate)}")
    print()
    print(f"  Stored in DB       : {r.stored_count}")
    print(
        f"  Coverage gaps      : {r.coverage_gap_count}"
        "  ((none) rows marked INVALID = missed startups)"
    )
    print(f"  Avg Qwen time      : {_fmt(r.avg_qwen_duration_s)}s")
    print()
    print("  Startups per page:")
    for page, counts in sorted(r.per_page_breakdown.items()):
        print(f"    {page[:70]}")
        print(
            f"      valid={counts['valid']}  invalid={counts['invalid']}  "
            f"partial={counts['partial']}  stored={counts['stored']}"
        )
    if r.reviewer_notes_by_verdict:
        print()
        print("  Reviewer notes:")
        for verdict, notes in r.reviewer_notes_by_verdict.items():
            print(f"    [{verdict}]")
            for note in notes[:10]:
                print(f"      · {note}")
    print(sep + "\n")
