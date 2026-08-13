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
