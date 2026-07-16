"""Phase S-2 tuning loader + candidate filter (hot-reload, safe fallback)."""
import shutil
import time
import pytest

from config.tuning_loader import (
    get_extraction_rules, get_candidate_filter_config, get_scoring_config, TUNING_YAML_PATH,
)


@pytest.fixture
def restore_tuning():
    """Snapshot config/tuning.yaml and restore it after the test, forcing the
    loader to re-read the restored file so its in-memory 'last-known-good' cache
    is never left holding a modified config for the next test."""
    import os
    backup = TUNING_YAML_PATH + ".pytest.bak"
    shutil.copy(TUNING_YAML_PATH, backup)
    yield
    shutil.copy(backup, TUNING_YAML_PATH)
    os.remove(backup)
    time.sleep(0.02)
    # Force a re-parse of the restored (valid) file to refresh last-known-good.
    get_candidate_filter_config()
    get_scoring_config()


def test_defaults_present():
    assert get_extraction_rules()["include"]
    assert len(get_extraction_rules()["exclude"]) >= 1
    cf = get_candidate_filter_config()
    assert cf["min_score"] == 2 and cf["min_words"] == 25
    assert set(cf["signals"]) == {"startup", "geo", "tech", "company"}
    sc = get_scoring_config()
    assert sc["source_type_score"]["accelerator"] == 20
    assert sc["tiers"][0] == [81, "PRIORITY"]


GOOD = ("DeepDrive GmbH is a Munich-based startup founded in 2020 that raised a seed round "
        "for its autonomous driving software platform used by logistics companies across Germany.")


def test_candidate_filter_relevance():
    from ingestion.candidate_filter import is_relevant
    assert is_relevant(GOOD) is True
    assert is_relevant("Home About Contact Imprint Privacy Cookies Menu Login Search Help") is False


def test_candidate_filter_hot_reload(restore_tuning):
    from ingestion.candidate_filter import is_relevant
    assert is_relevant(GOOD) is True
    with open(TUNING_YAML_PATH) as f:
        content = f.read()
    with open(TUNING_YAML_PATH, "w") as f:
        f.write(content.replace("min_words: 25", "min_words: 200"))
    time.sleep(0.05)
    assert is_relevant(GOOD) is False  # threshold change took effect with no restart


def test_broken_yaml_falls_back(restore_tuning):
    from ingestion.candidate_filter import is_relevant
    # Warm up: ensure the current GOOD file is the loader's last-known-good.
    assert is_relevant(GOOD) is True
    with open(TUNING_YAML_PATH, "w") as f:
        f.write(": : broken [[[\n")
    time.sleep(0.05)
    # filter and scoring still work on last-known-good / defaults
    assert is_relevant(GOOD) is True
    assert get_scoring_config()["source_type_score"]["accelerator"] == 20
