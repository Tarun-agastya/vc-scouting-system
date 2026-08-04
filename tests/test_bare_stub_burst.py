"""
Per-source anomaly circuit breaker (4 Aug, review-inbox-flooding audit).

A single run's PipelineMetrics can now detect a BURST of evidence-free new
masters (no description, no website) from one source — exactly the shape of
the hochschule-biberach.de incident (124 fake "startups" from a
misdetected photo-gallery "logo grid"). Never blocks anything — just makes
a human aware the run's output is worth a second look.
"""
from ingestion.worker_queue import PipelineMetrics


def test_no_burst_below_absolute_floor():
    m = PipelineMetrics()
    m.startups_inserted = 10
    m.bare_stub_new_masters = 5  # below the default min_count=15
    assert not m.bare_stub_burst()


def test_no_burst_below_fraction_even_with_high_count():
    m = PipelineMetrics()
    m.startups_inserted = 100
    m.bare_stub_new_masters = 20  # >= min_count but only 20% of a healthy run
    assert not m.bare_stub_burst()


def test_burst_detected_when_both_thresholds_cleared():
    m = PipelineMetrics()
    m.startups_inserted = 24
    m.bare_stub_new_masters = 20  # >= 15 absolute, and >= 60% of inserted
    assert m.bare_stub_burst()


def test_burst_thresholds_are_configurable():
    m = PipelineMetrics()
    m.startups_inserted = 10
    m.bare_stub_new_masters = 8
    assert not m.bare_stub_burst()  # below default floor of 15
    assert m.bare_stub_burst(min_count=5, min_fraction=0.5)
