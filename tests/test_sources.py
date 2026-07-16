"""Phase S dynamic source registry: load, add, delete, malformed-skip, round-trip."""
import shutil
import pytest

from config import source_loader as SL


@pytest.fixture
def restore_sources():
    backup = SL.SOURCES_YAML_PATH + ".pytest.bak"
    shutil.copy(SL.SOURCES_YAML_PATH, backup)
    yield
    shutil.copy(backup, SL.SOURCES_YAML_PATH)
    import os
    os.remove(backup)


def test_load_seeded_registry():
    assert len(SL.get_rss_feeds()) >= 1
    assert len(SL.get_web_sources()) >= 1
    assert len(SL.get_high_priority_sources()) >= 1
    # every web source has the required identity fields
    for s in SL.get_web_sources():
        assert s.source_id and s.source_name and s.primary_url


def test_add_then_delete_round_trip(restore_sources):
    before = len(SL.get_web_sources())
    entry = SL.add_web_source({
        "source_id": "pytest_src", "source_name": "PYTEST Source",
        "source_type": "accelerator", "primary_url": "https://pytest.example/portfolio",
    })
    assert entry.source_id == "pytest_src"
    assert len(SL.get_web_sources()) == before + 1
    assert SL.delete_source("pytest_src") is True
    assert len(SL.get_web_sources()) == before
    assert SL.delete_source("pytest_src") is False  # idempotent-safe


def test_duplicate_source_id_rejected(restore_sources):
    SL.add_web_source({"source_id": "pytest_dup", "source_name": "PYTEST A",
                       "source_type": "accelerator", "primary_url": "https://a.example"})
    with pytest.raises(ValueError):
        SL.add_web_source({"source_id": "pytest_dup", "source_name": "PYTEST B",
                           "source_type": "accelerator", "primary_url": "https://b.example"})


def test_malformed_entry_skipped(restore_sources):
    # append a web_source missing the required source_type; loader must skip it
    with open(SL.SOURCES_YAML_PATH, "a") as f:
        f.write("\n  - source_id: pytest_broken\n    source_name: PYTEST Broken\n")
    ids = {s.source_id for s in SL.get_web_sources()}
    assert "pytest_broken" not in ids  # skipped, didn't crash the load
