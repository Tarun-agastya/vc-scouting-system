"""
processing/verifier.py::_layer1_reground -- regression for a bug found live
13 Aug 2026: the module's own docstring promises H-1's 5 deterministically-
gated fields are re-checked on every recheck ("founded_year, funding_stage,
funding_amount, employee_count, founders"), but funding_amount was built
into the grounding snapshot and then silently dropped -- never compared,
never added to the `changes` dict returned to the caller, so a fabricated
funding_amount unsupported by source_excerpt survived Phase H-3 recheck
untouched despite the audit trail implying it had been checked.
"""
from unittest.mock import patch

from processing.verifier import _layer1_reground


class _FakeRecord:
    def __init__(self, **kw):
        self.founded_year = kw.get("founded_year")
        self.funding_stage = kw.get("funding_stage")
        self.employee_count = kw.get("employee_count")
        self.raw_data = kw.get("raw_data") or {}
        self.source_excerpt = kw.get("source_excerpt", "some source text")


def test_layer1_reground_reports_a_nulled_funding_amount():
    record = _FakeRecord(
        founded_year=2020, funding_stage="Seed", employee_count=10,
        raw_data={"funding_amount": "$5M", "founders": []},
    )
    grounded = {
        "founded_year": 2020, "funding_stage": "Seed", "employee_count": 10,
        "funding_amount": None,  # grounding gate decided this is unsupported
        "founders": [],
    }
    with patch("config.tuning_loader.get_grounding_config", return_value={"enabled": True}), \
         patch("reasoning.qwen_client._ground_startup", return_value=grounded):
        changes = _layer1_reground(record)

    assert changes.get("funding_amount") is None
    assert "funding_amount" in changes, "a nulled funding_amount must be reported, not silently dropped"


def test_layer1_reground_omits_funding_amount_when_unchanged():
    record = _FakeRecord(
        founded_year=2020, funding_stage="Seed", employee_count=10,
        raw_data={"funding_amount": "$5M", "founders": []},
    )
    grounded = {
        "founded_year": 2020, "funding_stage": "Seed", "employee_count": 10,
        "funding_amount": "$5M", "founders": [],
    }
    with patch("config.tuning_loader.get_grounding_config", return_value={"enabled": True}), \
         patch("reasoning.qwen_client._ground_startup", return_value=grounded):
        changes = _layer1_reground(record)

    assert "funding_amount" not in changes


# ── _ground_startup: founders must never be iterated as characters ──────────
# Root cause of the founders-shredding corruption, proven live 16 Aug 2026.
# _ground_startup's founders loop iterated s["founders"] directly, so a bare
# STRING (which the LLM's own output, a manual /add-startup payload, or an
# already-corrupted raw_data row can all supply) got walked character by
# character -- every single letter appearing as a standalone token in the
# source text "passed" the surname check and was kept, producing the real
# ['A','t','e','S','t','t','e'] rows found in the database. This ran on every
# ingest AND every nightly recheck, so it kept manufacturing new corruption
# even after the 14 Aug data repair.

_GROUND_CFG = {
    "check_founders": True, "check_founded_year": False,
    "check_employee_count": False, "check_funding_amount": False,
}
_SRC = "Anne Sraders is a senior reporter. Ate Stte founded the company. A t e S."


def test_ground_startup_does_not_shred_a_bare_string_founders():
    from reasoning.qwen_client import _ground_startup
    out = _ground_startup({"name": "T", "founders": "Anne Sraders"}, _SRC, _GROUND_CFG)
    assert out["founders"] == ["Anne Sraders"], (
        "a bare string must be treated as ONE founder name, never iterated into characters"
    )
    assert not any(len(f) == 1 for f in out["founders"])


def test_ground_startup_leaves_a_correct_list_alone():
    from reasoning.qwen_client import _ground_startup
    out = _ground_startup({"name": "T", "founders": ["Anne Sraders"]}, _SRC, _GROUND_CFG)
    assert out["founders"] == ["Anne Sraders"]


def test_ground_startup_still_drops_a_founder_absent_from_the_source():
    """The grounding gate's actual job must survive the coercion fix."""
    from reasoning.qwen_client import _ground_startup
    out = _ground_startup({"name": "T", "founders": ["Zzzz Nonexistent"]}, _SRC, _GROUND_CFG)
    assert out["founders"] == []
