"""
The nightly resolver (processing/review_resolver.py) — Phase 2 of
plans/REVIEW_INBOX_AUTONOMY_PLAN.md.

These test the ORCHESTRATION, not the model's judgement — the adjudicators
themselves are monkeypatched to a controlled fake, same approach as
tests/test_reviews_grouped.py. What must not drift here:
  - competing proposals for one (master, field) are judged TOGETHER, never
    one review at a time (the 12 Aug incident this whole plan is built
    around — see processing/review_resolver.py's docstring);
  - the live master value is read at judgement time, never a review's
    frozen `old` snapshot;
  - apply=True only ever closes the reversible direction (reject/suppress),
    never overwrites a field or merges a record, whatever the confidence;
  - report-only (apply=False) writes reasoning but closes nothing.

Every call below passes master_ids=[...] to scope the resolver to the
test's own records. resolve_pending() otherwise scans the WHOLE pending
queue by design (that's its job) — this suite runs against the live
database like every other test here, and a monkeypatched adjudicator
returning a canned verdict must never be allowed to touch the ~500 real
pending reviews sitting alongside the test data.
"""
import asyncio

import processing.field_adjudicator as field_adjudicator
import processing.dedup_adjudicator as dedup_adjudicator
from database.models import DuplicateReview, SuppressedMatch
from processing.review_resolver import resolve_pending


def _pending(db, master_id, review_type="field_update"):
    return db.query(DuplicateReview).filter(
        DuplicateReview.master_id == master_id,
        DuplicateReview.review_type == review_type,
        DuplicateReview.status == "pending",
    ).all()


def _stage_field_update(db, master_id, master_name, field, old, new):
    r = DuplicateReview(
        review_type="field_update", master_id=master_id, master_name=master_name,
        incoming_name=master_name,
        proposed_changes={field: {"old": old, "new": new, "incoming_source": "pytest"}},
        risk_level="low", status="pending", source="pytest",
    )
    db.add(r)
    db.commit()
    return r


# ── field_update: grouping is the safety property ──────────────────────────

def test_competing_proposals_judged_in_one_call_not_one_per_review(make, db, monkeypatch):
    """
    The exact shape of the 12 Aug incident: three pending reviews propose
    THREE DIFFERENT values for the same (master, field). If the resolver
    judged them one at a time, the last one processed could silently win —
    grouping them into a single adjudicate_field_group call is what
    prevents that.
    """
    rid, _ = make("Resolver Group", website="pytest-resolver-group.com", city="Munich",
                  description="widget maker")
    _stage_field_update(db, rid, "PYTEST Resolver Group", "sub_industry", None, "A")
    _stage_field_update(db, rid, "PYTEST Resolver Group", "sub_industry", None, "B")
    _stage_field_update(db, rid, "PYTEST Resolver Group", "sub_industry", None, "C")
    assert len(_pending(db, rid)) == 3

    calls = []

    def fake_group(record, field, current, candidates, **kw):
        calls.append((field, current, sorted(candidates)))
        return {"winner": None, "none_fit": False, "confidence": "high",
                "reasoning": "test", "considered": candidates, "model": "test"}

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", fake_group)

    asyncio.run(resolve_pending(limit=50, apply=False, only_type="field_update",
                                master_ids=[rid]))

    assert len(calls) == 1  # ONE call for the group, not three
    assert calls[0][0] == "sub_industry"
    assert calls[0][2] == ["A", "B", "C"]  # every competing candidate seen together

    db.expire_all()
    reviews = _pending(db, rid)
    # report-only: still pending, but every member carries the SAME reasoning —
    # no contradictory verdicts across siblings.
    assert len(reviews) == 3
    explanations = {r.llm_explanation for r in reviews}
    assert len(explanations) == 1


def test_uses_the_live_master_value_not_the_reviews_frozen_old(make, db, monkeypatch):
    """
    A review's `old` is a snapshot frozen when it was created. If the master
    changed since, judging against the stale snapshot is exactly the 12 Aug
    bug's other half. The resolver must re-read the live column.
    """
    rid, _ = make("Resolver Live", website="pytest-resolver-live.com", city="Munich",
                  description="widget maker", funding_stage="Seed")
    # Snapshot says "old" was empty — stale; the master has moved on since.
    _stage_field_update(db, rid, "PYTEST Resolver Live", "funding_stage", None, "Pre-Seed")

    seen_current = []

    def fake_group(record, field, current, candidates, **kw):
        seen_current.append(current)
        return {"winner": None, "none_fit": False, "confidence": "high",
                "reasoning": "test", "considered": candidates, "model": "test"}

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", fake_group)

    asyncio.run(resolve_pending(limit=50, apply=False, only_type="field_update",
                                master_ids=[rid]))

    assert seen_current == ["Seed"]  # the LIVE value, not the review's stale "old": None


def test_tags_and_founders_are_never_adjudicated_as_a_scalar_field(make, db, monkeypatch):
    """
    Found running this against real data (29 Sep): a review can propose
    several fields at once, and a MIXED row (a scalar field like sub_industry
    alongside tags/founders) is never touched by
    drain_list_field_reviews.py's all-list-fields-only drain. Feeding a
    whole list as adjudicate_field_group's "candidate" produced a garbled
    str(list) and a confused verdict — tags/founders must never reach it.
    """
    rid, _ = make("Resolver Mixed", website="pytest-resolver-mixed.com", city="Munich",
                  description="widget maker")
    r = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Resolver Mixed",
        incoming_name="PYTEST Resolver Mixed",
        proposed_changes={
            "sub_industry": {"old": "Fintech", "new": "HealthTech"},
            "tags": {"old": [], "new": ["B2B SaaS", "HR Tech", "startup hub"]},
        },
        risk_level="low", status="pending", source="pytest",
    )
    db.add(r)
    db.commit()

    seen_fields = []

    def fake_group(record, field, current, candidates, **kw):
        seen_fields.append(field)
        return {"winner": None, "none_fit": False, "confidence": "high",
                "reasoning": "test", "considered": candidates, "model": "test"}

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", fake_group)

    asyncio.run(resolve_pending(limit=50, apply=False, only_type="field_update",
                                master_ids=[rid]))

    assert seen_fields == ["sub_industry"]  # tags never passed to the adjudicator


def test_evidence_for_every_field_survives_on_a_multi_field_review(make, db, monkeypatch):
    """
    The other half of the same finding: a review proposing TWO scalar
    fields (city AND funding_stage) belongs to two separate groups. Before
    this fix, writing evidence for the second group processed silently
    overwrote the first group's evidence and llm_explanation on the shared
    review row — an unattended job must not lose a reviewer's reasoning
    like that.
    """
    rid, _ = make("Resolver TwoFields", website="pytest-resolver-twofields.com",
                  city="Munich", description="widget maker", funding_stage="Seed")
    r = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Resolver TwoFields",
        incoming_name="PYTEST Resolver TwoFields",
        proposed_changes={
            "city": {"old": "Munich", "new": "Berlin"},
            "funding_stage": {"old": "Seed", "new": "Pre-Seed"},
        },
        risk_level="low", status="pending", source="pytest",
    )
    db.add(r)
    db.commit()

    def fake_group(record, field, current, candidates, **kw):
        return {"winner": None, "none_fit": False, "confidence": "high",
                "reasoning": f"reasoning for {field}", "considered": candidates, "model": "test"}

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", fake_group)

    asyncio.run(resolve_pending(limit=50, apply=False, only_type="field_update",
                                master_ids=[rid]))

    db.expire_all()
    rev = db.query(DuplicateReview).filter(DuplicateReview.id == r.id).first()
    field_adjs = rev.evidence.get("field_adjudications", {})
    assert set(field_adjs) == {"city", "funding_stage"}  # BOTH survived
    assert "reasoning for city" in rev.llm_explanation
    assert "reasoning for funding_stage" in rev.llm_explanation


# ── field_update: apply only closes the reversible direction ───────────────

def test_apply_closes_keep_current_high_confidence(make, db, monkeypatch):
    rid, _ = make("Resolver Keep", website="pytest-resolver-keep.com", city="Munich",
                  description="widget maker")
    _stage_field_update(db, rid, "PYTEST Resolver Keep", "sub_industry", "Fintech", "HealthTech")

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", lambda *a, **k: {
        "winner": None, "none_fit": False, "confidence": "high",
        "reasoning": "test", "considered": ["HealthTech"], "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                master_ids=[rid]))

    db.expire_all()
    assert _pending(db, rid) == []  # closed
    sup = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "rejected_value", SuppressedMatch.master_id == rid,
        SuppressedMatch.field == "sub_industry", SuppressedMatch.value == "HealthTech",
    ).first()
    assert sup is not None


def test_apply_never_overwrites_even_at_high_confidence(make, db, monkeypatch):
    """A winning proposal is never auto-applied. This is the whole point of
    the asymmetric policy — see processing/field_adjudicator.py."""
    import processing.trust as trust
    # Independent of the LIVE ledger: once real humans have agreed often
    # enough, sub_industry legitimately earns auto-apply (A4) — this test is
    # about the default, un-earned state, so pin it.
    monkeypatch.setattr(trust, "field_autonomy", lambda db, field: False)
    rid, _ = make("Resolver Winner", website="pytest-resolver-winner.com", city="Munich",
                  description="widget maker", sub_industry="Fintech")
    _stage_field_update(db, rid, "PYTEST Resolver Winner", "sub_industry", "Fintech", "HealthTech")

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", lambda *a, **k: {
        "winner": "HealthTech", "none_fit": False, "confidence": "high",
        "reasoning": "test", "considered": ["HealthTech"], "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                master_ids=[rid]))

    db.expire_all()
    reviews = _pending(db, rid)
    assert len(reviews) == 1  # still pending for a human
    from database.models import Startup
    assert db.query(Startup).filter(Startup.id == rid).first().sub_industry == "Fintech"  # untouched


def test_apply_never_closes_none_fit(make, db, monkeypatch):
    rid, _ = make("Resolver NoneFit", website="pytest-resolver-nonefit.com", city="Munich",
                  description="widget maker")
    _stage_field_update(db, rid, "PYTEST Resolver NoneFit", "sub_industry", "Fintech", "HealthTech")

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", lambda *a, **k: {
        "winner": None, "none_fit": True, "confidence": "high",
        "reasoning": "neither fits", "considered": ["HealthTech"], "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                master_ids=[rid]))

    db.expire_all()
    assert len(_pending(db, rid)) == 1  # left for a human, not silently closed


def test_report_only_writes_reasoning_but_closes_nothing(make, db, monkeypatch):
    rid, _ = make("Resolver Report", website="pytest-resolver-report.com", city="Munich",
                  description="widget maker")
    _stage_field_update(db, rid, "PYTEST Resolver Report", "sub_industry", "Fintech", "HealthTech")

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", lambda *a, **k: {
        "winner": None, "none_fit": False, "confidence": "high",
        "reasoning": "the stored value fits", "considered": ["HealthTech"], "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=False, only_type="field_update",
                                master_ids=[rid]))

    db.expire_all()
    reviews = _pending(db, rid)
    assert len(reviews) == 1  # apply=False — nothing closes
    assert "the stored value fits" in reviews[0].llm_explanation
    field_adjs = reviews[0].evidence.get("field_adjudications", {})
    assert field_adjs.get("sub_industry", {}).get("confidence") == "high"


# ── possible_duplicate: never merges, only suppresses ───────────────────────

def test_duplicate_apply_suppresses_different_company(make, db, monkeypatch):
    r1, _ = make("Resolver Dup A", website="pytest-resolver-dupa.com", city="Paris")
    r2, _ = make("Resolver Dup B", website="pytest-resolver-dupb.com", city="Tokyo")
    rev = DuplicateReview(
        review_type="possible_duplicate", master_id=r1, master_name="PYTEST Resolver Dup A",
        incoming_id=r2, incoming_name="PYTEST Resolver Dup B",
        risk_level="low", status="pending", source="pytest",
    )
    db.add(rev)
    db.commit()

    monkeypatch.setattr(dedup_adjudicator, "adjudicate_pair", lambda *a, **k: {
        "verdict": "different_company", "confidence": "high",
        "key_signal": "city", "reasoning": "test", "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="possible_duplicate",
                                master_ids=[r1]))

    db.expire_all()
    assert _pending(db, r1, "possible_duplicate") == []
    sup = db.query(SuppressedMatch).filter(
        SuppressedMatch.kind == "known_different",
        SuppressedMatch.master_id == r1, SuppressedMatch.other_id == r2,
    ).first()
    assert sup is not None
    # both records still exist — suppression, not deletion
    from database.models import Startup
    assert db.query(Startup).filter(Startup.id.in_([r1, r2])).count() == 2


def test_duplicate_never_auto_merges_same_company(make, db, monkeypatch):
    """same_company is never auto-applied at any confidence — merging
    deletes a record and is the expensive mistake this database has
    actually suffered from over-merging, not under-merging."""
    r1, _ = make("Resolver Merge A", website="pytest-resolver-mergea.com", city="Paris",
                 description="alpha widget maker")
    r2, _ = make("Resolver Merge B", website="pytest-resolver-mergeb.com", city="Paris",
                 description="beta widget maker")
    rev = DuplicateReview(
        review_type="possible_duplicate", master_id=r1, master_name="PYTEST Resolver Merge A",
        incoming_id=r2, incoming_name="PYTEST Resolver Merge B",
        risk_level="low", status="pending", source="pytest",
    )
    db.add(rev)
    db.commit()

    monkeypatch.setattr(dedup_adjudicator, "adjudicate_pair", lambda *a, **k: {
        "verdict": "same_company", "confidence": "high",
        "key_signal": "name", "reasoning": "looks identical", "model": "test"})

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="possible_duplicate",
                                master_ids=[r1]))

    db.expire_all()
    # >=1, not ==1: two near-identically-named test records can ALSO get
    # flagged as a possible duplicate by the real matcher independent of the
    # one we staged by hand — irrelevant here; what matters is that not one
    # of the master_id=r1 reviews was closed.
    assert len(_pending(db, r1, "possible_duplicate")) >= 1
    from database.models import Startup
    assert db.query(Startup).filter(Startup.id.in_([r1, r2])).count() == 2  # neither merged nor deleted


# ── degrades cleanly when the model is unavailable ──────────────────────────

def test_consecutive_failures_stop_the_run_without_raising(make, db, monkeypatch):
    rid, _ = make("Resolver Down", website="pytest-resolver-down.com", city="Munich",
                  description="widget maker")
    _stage_field_update(db, rid, "PYTEST Resolver Down", "sub_industry", "Fintech", "HealthTech")

    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group", lambda *a, **k: None)

    stats = asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                        master_ids=[rid]))

    assert stats.get("unavailable", 0) >= 1
    assert "error" not in stats  # never raises — a None result is expected, handled input
    db.expire_all()
    assert len(_pending(db, rid)) == 1  # untouched, will retry next run


def test_mixed_review_has_its_list_part_merged_and_keeps_the_scalar_question(make, db):
    """34 of 125 pending rows mixed a lossless tags union with a real scalar
    question, so the all-list-only drain skipped them whole."""
    from database.models import Startup
    from processing.list_field_drain import drain_mixed_list_fields

    rid, _ = make("Resolver Split", website="pytest-resolver-split.com", city="Munich",
                  description="widget maker", tags=["fintech"])
    r = DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Resolver Split",
        incoming_name="x", risk_level="low", status="pending", source="pytest",
        proposed_changes={"city": {"old": "Munich", "new": "Berlin"},
                          "tags": {"old": ["fintech"], "new": ["fintech", "payments"]}})
    db.add(r)
    db.commit()
    review_id = r.id

    stats = drain_mixed_list_fields(db, apply=True, master_ids=[rid])

    assert stats["lists_merged"] == 1
    db.expire_all()
    assert set(db.query(Startup).filter(Startup.id == rid).first().tags) == {"fintech", "payments"}
    rev = db.query(DuplicateReview).get(review_id)
    assert rev.status == "pending"                       # the city question is still a human's
    assert set(rev.proposed_changes) == {"city"}


# ── A4: the one place the resolver may overwrite — and only when earned ─────

def _winner_verdict(w="HealthTech"):
    return {"winner": w, "none_fit": False, "confidence": "high",
            "reasoning": "test", "considered": [w], "model": "test"}


def test_an_earned_field_applies_the_winning_value_and_is_reversible(make, db, monkeypatch):
    import processing.trust as trust
    from database.models import DecisionAudit, FieldChange, Startup

    monkeypatch.setattr(trust, "field_autonomy", lambda db, field: True)
    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group",
                        lambda *a, **k: _winner_verdict())
    rid, _ = make("Resolver Earned", website="pytest-resolver-earned.com", city="Munich",
                  description="widget maker", sub_industry="Fintech")
    r = _stage_field_update(db, rid, "PYTEST Resolver Earned", "sub_industry", "Fintech", "HealthTech")
    rival = _stage_field_update(db, rid, "PYTEST Resolver Earned", "sub_industry", "Fintech", "Payments")
    ids = (r.id, rival.id)

    stats = asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                        master_ids=[rid]))

    assert stats["auto_applied"] == 2
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().sub_industry == "HealthTech"
    won, lost = (db.query(DuplicateReview).get(i) for i in ids)
    assert won.status == "approved" and "auto_applied" in won.evidence
    assert lost.status == "rejected"                              # the rival is suppressed
    # Reversible: the old value is in the record's history, attributed to us.
    ch = db.query(FieldChange).filter(FieldChange.startup_id == rid,
                                      FieldChange.field == "sub_industry").first()
    assert ch.source == "resolver" and ch.old_value == "Fintech" and ch.new_value == "HealthTech"
    # And the model was NOT graded on its own decision.
    assert db.query(DecisionAudit).filter(DecisionAudit.master_id == rid).count() == 0


def test_a_field_that_has_not_earned_it_is_never_overwritten(make, db, monkeypatch):
    import processing.trust as trust
    from database.models import Startup

    monkeypatch.setattr(trust, "field_autonomy", lambda db, field: False)
    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group",
                        lambda *a, **k: _winner_verdict())
    rid, _ = make("Resolver Unearned", website="pytest-resolver-unearned.com", city="Munich",
                  description="widget maker", sub_industry="Fintech")
    _stage_field_update(db, rid, "PYTEST Resolver Unearned", "sub_industry", "Fintech", "HealthTech")

    stats = asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update",
                                        master_ids=[rid]))
    assert stats.get("auto_applied", 0) == 0
    db.expire_all()
    assert db.query(Startup).filter(Startup.id == rid).first().sub_industry == "Fintech"
    assert len(_pending(db, rid)) == 1


def test_earned_never_applies_when_the_review_carries_another_field_too(make, db, monkeypatch):
    """Approving a multi-field review would approve the OTHER field with it,
    which the model never judged."""
    import processing.trust as trust
    from database.models import Startup

    monkeypatch.setattr(trust, "field_autonomy", lambda db, field: True)
    monkeypatch.setattr(field_adjudicator, "adjudicate_field_group",
                        lambda *a, **k: _winner_verdict())
    rid, _ = make("Resolver EarnedMulti", website="pytest-resolver-earnedmulti.com",
                  city="Munich", description="widget maker", sub_industry="Fintech")
    db.add(DuplicateReview(
        review_type="field_update", master_id=rid, master_name="PYTEST Resolver EarnedMulti",
        incoming_name="x", risk_level="low", status="pending", source="pytest",
        proposed_changes={"sub_industry": {"old": "Fintech", "new": "HealthTech"},
                          "city": {"old": "Munich", "new": "Berlin"}}))
    db.commit()

    asyncio.run(resolve_pending(limit=50, apply=True, only_type="field_update", master_ids=[rid]))
    db.expire_all()
    row = db.query(Startup).filter(Startup.id == rid).first()
    assert row.sub_industry == "Fintech" and row.city == "Munich"     # neither touched


def test_identity_fields_can_never_be_earned(make, db):
    """The real gate, unpatched: website is excluded whatever the ledger says."""
    from processing.trust import field_autonomy
    assert field_autonomy(db, "website") is False and field_autonomy(db, "name") is False
