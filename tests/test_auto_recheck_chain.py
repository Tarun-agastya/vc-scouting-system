"""
Phase AR-1 (12 Aug 2026, owner) — chain a LOCAL-ONLY recheck (H-3) after
RSS/newsletter/web ingestion instead of relying solely on the 03:00 nightly
job, so a new startup gets checked same-day. Deliberately never chains
web-verify (that costs a real search call and stays manual/nightly).

Mirrors tests/test_phase_x.py::test_chain_lives_above_execute_not_inside_a_work_function
for the same non-reentrant-mutex reason: the chain must live in the public
run_* method, never inside a _work_* function.
"""
import asyncio
import inspect

from processing.scout_controller import ScoutController


# ── structural: chain lives above _execute, never inside a _work_* function ─

def test_rss_chain_calls_run_rss_then_run_recheck():
    src = inspect.getsource(ScoutController.run_rss_then_recheck)
    assert "self.run_rss(" in src
    assert "self.run_recheck(" in src
    assert "self.run_web_verify" not in src, "must never chain a search-costing web-verify automatically"


def test_newsletters_chain_calls_run_newsletters_then_run_recheck():
    src = inspect.getsource(ScoutController.run_newsletters_then_recheck)
    assert "self.run_newsletters(" in src
    assert "self.run_recheck(" in src
    assert "self.run_web_verify" not in src


def test_web_source_chain_now_also_calls_run_recheck():
    """run_web_source_then_verify already chained web_verify_stubs (Phase
    X-4) -- confirm the new recheck chain was added alongside it, not in
    place of it."""
    src = inspect.getsource(ScoutController.run_web_source_then_verify)
    assert "self.run_web_source(" in src
    assert "self.run_web_verify_stubs(" in src
    assert "self.run_recheck(" in src


def test_no_work_function_calls_a_run_method():
    """Regression guard, same invariant test_phase_x.py already established:
    a _work_* function calling a run_* method re-enters _execute's
    non-reentrant gpu_mutex and deadlocks the whole pipeline."""
    for name, fn in inspect.getmembers(ScoutController, inspect.isfunction):
        if not name.startswith("_work_"):
            continue
        body = inspect.getsource(fn)
        assert "self.run_" not in body, f"{name} calls a run_* method — would deadlock"


# ── behavioral: only chains when the run actually found/stored something ───

class _FakeRec:
    def __init__(self, status="completed", metrics=None):
        self.status = status
        self.metrics = metrics or {}


def test_rss_chain_skips_recheck_when_nothing_extracted(monkeypatch):
    controller = ScoutController()
    calls = []

    async def fake_run_rss(**kwargs):
        return _FakeRec(metrics={"startups_extracted": 0})

    async def fake_run_recheck(**kwargs):
        calls.append(kwargs)
        return _FakeRec()

    monkeypatch.setattr(controller, "run_rss", fake_run_rss)
    monkeypatch.setattr(controller, "run_recheck", fake_run_recheck)

    asyncio.run(controller.run_rss_then_recheck())
    assert calls == [], "an RSS run that found nothing must not trigger a recheck call"


def test_rss_chain_fires_recheck_when_something_extracted(monkeypatch):
    controller = ScoutController()
    calls = []

    async def fake_run_rss(**kwargs):
        return _FakeRec(metrics={"startups_extracted": 5})

    async def fake_run_recheck(**kwargs):
        calls.append(kwargs)
        return _FakeRec()

    monkeypatch.setattr(controller, "run_rss", fake_run_rss)
    monkeypatch.setattr(controller, "run_recheck", fake_run_recheck)

    asyncio.run(controller.run_rss_then_recheck())
    assert len(calls) == 1
    assert calls[0]["limit"] == 20


def test_rss_chain_skips_recheck_when_run_did_not_complete(monkeypatch):
    """A stopped/errored RSS run must not trigger a follow-up recheck even
    if its (possibly partial) metrics happen to show a nonzero count."""
    controller = ScoutController()
    calls = []

    async def fake_run_rss(**kwargs):
        return _FakeRec(status="error", metrics={"startups_extracted": 3})

    async def fake_run_recheck(**kwargs):
        calls.append(kwargs)
        return _FakeRec()

    monkeypatch.setattr(controller, "run_rss", fake_run_rss)
    monkeypatch.setattr(controller, "run_recheck", fake_run_recheck)

    asyncio.run(controller.run_rss_then_recheck())
    assert calls == []


def test_newsletters_chain_fires_recheck_when_something_stored(monkeypatch):
    controller = ScoutController()
    calls = []

    async def fake_run_newsletters(**kwargs):
        return _FakeRec(metrics={"startups_stored": 2})

    async def fake_run_recheck(**kwargs):
        calls.append(kwargs)
        return _FakeRec()

    monkeypatch.setattr(controller, "run_newsletters", fake_run_newsletters)
    monkeypatch.setattr(controller, "run_recheck", fake_run_recheck)

    asyncio.run(controller.run_newsletters_then_recheck())
    assert len(calls) == 1


def test_web_source_chain_uses_a_smaller_recheck_limit_than_rss():
    """run_web_source_then_verify runs once PER SOURCE (up to ~19x in one
    sweep) -- its recheck limit must stay smaller than the once-per-batch
    RSS/newsletter chains' limit=20, or a full sweep's total added GPU time
    multiplies far more than intended."""
    src = inspect.getsource(ScoutController.run_web_source_then_verify)
    assert "self.run_recheck(limit=10" in src
