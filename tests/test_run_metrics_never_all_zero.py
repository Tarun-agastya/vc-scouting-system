"""
Every ingestion run kind must report metrics the dashboard can actually render.

WHY THIS TEST EXISTS. "The UI shows everything 0" was reported three separate
times by the owner. Each earlier fix addressed one producer and none stopped it
recurring, because the real defect was structural: RunRecord.to_dict() falls
back to the (empty until completion) `metrics` dict whenever `live_metrics` is
None, and the dashboard renders a fixed nine-tile grid regardless of run kind.
So any worker that forgot to attach live_metrics displayed a wall of zeros for
its entire duration, and any worker whose final metrics used key names outside
that grid displayed zeros even after succeeding.

Both were true at once:
  _work_rss          attached no live_metrics; final metrics had 1 of 9 keys
  _work_newsletters  attached no live_metrics; final metric was
                     `startups_stored`, which is not in the grid at all — so a
                     successful newsletter run read 0 on every single tile.

These tests pin the contract instead of the symptom, so a NEW run kind added
later cannot reintroduce it silently.
"""
import inspect
import re
from pathlib import Path

import pytest

from processing.scout_controller import RecordProgress, RunRecord, ScoutController, _metrics_to_dict

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "static" / "js" / "views" / "ingestion.js"

# The work functions that run a real ingestion job and therefore owe the
# dashboard live progress. Excluded: _work_web_verify_stubs (a chained helper,
# never its own run record).
JOB_WORKERS = [
    "_work_rss", "_work_web", "_work_newsletters", "_work_recheck",
    "_work_web_verify", "_work_recheck_selected", "_work_web_verify_selected",
    "_work_reclassify",
]


def _ui_metric_keys():
    """The metric keys the dashboard's tile grid actually reads."""
    src = UI_FILE.read_text(encoding="utf-8")
    block = src[src.index("const METRIC_LABELS"):src.index("];", src.index("const METRIC_LABELS"))]
    return set(re.findall(r'\["([a-z_]+)",', block))


@pytest.mark.parametrize("name", JOB_WORKERS)
def test_every_job_worker_attaches_live_metrics(name):
    """
    Without rec.live_metrics, to_dict() returns the empty `metrics` dict for
    the whole run and every tile reads 0 — the exact reported bug.
    """
    fn = getattr(ScoutController, name, None)
    assert fn is not None, f"{name} no longer exists — update JOB_WORKERS"
    src = inspect.getsource(fn)
    assert "rec.live_metrics" in src, (
        f"{name} never sets rec.live_metrics, so /ingestion/status reports no "
        f"progress while it runs and the dashboard shows a grid of zeros. "
        f"Attach a RecordProgress (record-by-record jobs) or a PipelineMetrics "
        f"(page crawls)."
    )


@pytest.mark.parametrize("name", JOB_WORKERS)
def test_every_job_worker_takes_the_run_record(name):
    """live_metrics can only be attached if the worker is handed the record."""
    fn = getattr(ScoutController, name)
    assert "rec" in inspect.signature(fn).parameters, (
        f"{name} does not receive `rec`, so it cannot publish live progress"
    )


def test_record_progress_renders_as_a_progress_bar_not_a_zero_grid():
    """
    The dashboard switches on `typeof m.total === "number"`. A RecordProgress
    must therefore always expose `total`, or it falls through to the nine-tile
    grid and shows zeros.
    """
    rec = RunRecord(run_id="t", kind="newsletter", source="x", status="running")
    rec.live_metrics = RecordProgress(total=42)
    rec.live_metrics.processed = 7
    m = rec.to_dict()["metrics"]
    assert isinstance(m.get("total"), int) and m["total"] == 42
    assert m["processed"] == 7


def test_rss_and_newsletter_final_metrics_are_renderable():
    """
    A finished run's metrics must contain at least one key the UI renders, or
    the run looks like it did nothing. `startups_stored` alone failed this —
    it is not in METRIC_LABELS.
    """
    ui_keys = _ui_metric_keys()
    assert "startups_extracted" in ui_keys, "sanity: the grid should read startups_extracted"

    for name in ("_work_rss", "_work_newsletters"):
        src = inspect.getsource(getattr(ScoutController, name))
        returned = set(re.findall(r'"([a-z_]+)":', src))
        # Either a key the grid shows, or the processed/total pair that makes
        # the progress-bar branch render instead. Both are honest; zeros are not.
        assert returned & (ui_keys | {"processed", "total"}), (
            f"{name} returns {sorted(returned)}, none of which the dashboard "
            f"renders — a successful run would display as all zeros"
        )


def test_empty_metrics_do_not_render_a_zero_grid():
    """
    The UI-side half of the guarantee: when a run reports nothing yet, the
    page must say so rather than draw nine zeros. Pinned because this is what
    makes the bug impossible for a future run kind nobody has written yet.
    """
    src = UI_FILE.read_text(encoding="utf-8")
    assert "hasAnyMetrics" in src, (
        "ingestion.js lost its empty-metrics guard — a run with no counters "
        "will render a grid of zeros again"
    )
    assert "waiting for the first counters" in src


def test_metrics_to_dict_never_returns_empty_for_a_real_metrics_object():
    """_metrics_to_dict must emit every grid key, so a web run shows real 0s
    (meaning 'measured zero') rather than missing keys."""
    from ingestion.worker_queue import PipelineMetrics

    out = _metrics_to_dict(PipelineMetrics())
    for key in _ui_metric_keys():
        assert key in out, f"{key} is rendered by the dashboard but absent from _metrics_to_dict"
