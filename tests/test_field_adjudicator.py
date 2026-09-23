"""
The field adjudicator's POLICY — the part that decides what a verdict is
allowed to do, tested without invoking a model.

The verdicts themselves are a model's judgement and are checked by running
the thing against real records. What must not drift is the asymmetry: the
model may say "keep what we already have" and act on it, and may never
overwrite a stored value.
"""
from processing.field_adjudicator import IDENTITY_FIELDS, VERDICTS, may_auto_apply


def _v(verdict, confidence="high"):
    return {"verdict": verdict, "confidence": confidence, "reasoning": "x", "model": "test"}


def test_only_keep_old_is_ever_auto_applied():
    """
    keep_old writes no field — it rejects the review, which is what a human
    clicking Reject does. take_new overwrites a stored value, and replacing a
    correct value with a worse one is the failure this database has actually
    suffered.
    """
    assert may_auto_apply(_v("keep_old"), "sub_industry") is True
    assert may_auto_apply(_v("take_new"), "sub_industry") is False
    assert may_auto_apply(_v("unsure"), "sub_industry") is False


def test_take_new_is_refused_even_at_high_confidence():
    """Not a confidence threshold — a direction that is never automatic."""
    for conf in ("high", "medium", "low"):
        assert may_auto_apply(_v("take_new", conf), "sub_industry") is False


def test_low_confidence_keep_old_is_not_applied():
    assert may_auto_apply(_v("keep_old", "medium"), "sub_industry") is False
    assert may_auto_apply(_v("keep_old", "low"), "sub_industry") is False


def test_identity_fields_are_never_auto_resolved():
    """
    website feeds the fingerprint, so changing it changes what the record IS —
    and a wrong fingerprint is what made 683 records invisible to dedup in
    September. Never automatic, in either direction.
    """
    for field in IDENTITY_FIELDS:
        assert may_auto_apply(_v("keep_old"), field) is False
        assert may_auto_apply(_v("take_new"), field) is False


def test_a_missing_or_unusable_verdict_does_nothing():
    assert may_auto_apply(None, "sub_industry") is False
    assert may_auto_apply({}, "sub_industry") is False


def test_the_verdict_vocabulary_is_fixed():
    """The runner branches on these; adding one silently would be ignored."""
    assert set(VERDICTS) == {"keep_old", "take_new", "unsure"}
