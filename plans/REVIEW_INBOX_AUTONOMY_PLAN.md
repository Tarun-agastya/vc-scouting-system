# Autonomous Review Inbox — a nightly resolver that actually decides

**Status:** drafted 24 Sep 2026 · awaiting owner review · nothing implemented yet
**Supersedes:** nothing. Builds on Phase Z (complete) and the two adjudicators shipped 22–23 Sep.

---

## Context

The Review Inbox is at **512 pending** and the owner asked to make it autonomous — specifically whether OpenClaw or an agent could take it over and orchestrate the whole thing.

Measured on the live DB before planning, and two things came back that change the shape of the answer:

### 1. Every decision-making part already exists and nothing runs it

| Piece | State |
|---|---|
| `processing/dedup_adjudicator.py` | Built, tested, **0 of the 267 pending duplicates have ever been judged** |
| `processing/field_adjudicator.py` | Built, fixed twice this week, run manually on 65 of 111 `sub_industry` groups |
| `scripts/adjudicate_*.py` | Dry-run runners, **not referenced by any plist or scheduler** |
| `processing/review_explainer.py` | Runs nightly 02:00 — but writes prose only, *never a verdict* |

So the gap is not intelligence. It is that **nothing closes the loop**: the adjudicators are pure functions that never write, and the only thing scheduled against the inbox deliberately refuses to decide.

### 2. The queue is refilling faster than anyone can drain it, because of a live bug

317 of the 512 pending reviews were created **on 24 Sep alone**. Breaking the 245 `field_update` rows down by field:

```
tags           119      sub_industry   111      founders        25
city            21      website         18      funding_stage   12
contact_info     5      country          5      address          3
```

`tags` + `founders` = **144 reviews that should never have been created.**

The 23 Sep fix routed list fields through `field_policy.merge_list_field` so a lossless union is applied instead of staged — but `_LIST_FIELDS` is checked *inside* `for attr, key in _DIFF_FIELDS.items()`, and **neither `tags` nor `founders` is in `_DIFF_FIELDS`**. The branch at `processing/storage.py:763` is unreachable dead code. The legacy block at `storage.py:803-811` still stages every list change unconditionally, which is why 74 new `tags` reviews appeared in one day. Sampled them: all pure supersets, exactly the lossless case the union was written for.

It shipped green because `tests/test_field_policy.py` tests `merge_list_field` as a pure function and **nothing tests `_diff_fields` end to end**. `drain_list_field_reviews.py` then cleared the backlog, so the queue visibly fell 637→211 and the creation path looked fixed.

**Automating a queue that refills at ~100/day is pointless, so the bug is Phase 1.**

---

## Why not OpenClaw / ElizaOS — and what "agent" correctly means here

`openclaw/` is two JSON files and no runtime: `character.json` (a Discord persona, SCOUT) and `tools_config.json` (6 HTTP tools, all *search* endpoints). Nothing in the repo reads either file. It is a conversational veneer over the REST API — it has no queue-draining ability, and `plans/ARCHITECTURE_REDESIGN_PLAN.md:11` records that running a 14B agent alongside the 14B pipeline on this one Mac mini is what caused cascading 120s timeouts.

**Owner decision (confirmed):** build the resolver as a Python job inside the existing app — no Node, no Discord, no second GPU consumer. The genuinely agentic part (search → read → decide) is Phase 3, where it earns its keep.

**Owner decisions (confirmed):**
- **Autonomy: reversible-only, but nightly.** Keep today's asymmetric policy exactly; just run it automatically. Auto-close `keep_current`/`different_company` at high confidence; everything else gets a verdict + reasoning attached for one-click human approval.
- **Research loop: yes, with a hard nightly search budget.**

---

## The one safety rule this plan is built around

Unchanged from Phase Z, and re-stated because a fourth automated writer is being added. The 12 Aug incident (`scripts/revert_multi_candidate_clobbers.py`) judged each review against its own frozen `old` snapshot, so where several pending reviews proposed different values for the same `(master, field)`, the last one processed won by query order — 130 fields across 92 startups. The resolver **must**:

1. Group by `(master_id, field)` across all pending rows **before** deciding anything.
2. Re-read the master's **live** value immediately before writing — never trust `change["old"]`.
3. Never auto-apply when >1 distinct candidate exists; `field_adjudicator.adjudicate_field_group` already judges competing proposals in one call and is the required entry point.

This failure mode recurred as recently as 23 Sep (the adjudicator kept `Fintech - Payments` on an antibiotics company while its own reasoning called the value unrelated), so the prompt driving it is **days old and not yet validated at scale**. That is the direct argument for reversible-only autonomy now, and why Phase 2 ships report-only first.

---

## Phase 1 — Stop the regrowth (kills 144 pending, ~74/day creation)

**`processing/storage.py`**

- Add `"tags": "tags"` to `_DIFF_FIELDS` so the existing `_LIST_FIELDS` branch actually executes and routes the union into `auto_apply`. `tags` is a real column (`ARRAY(String)`, `models.py:26`), so the caller's `setattr` path at `storage.py:189` works unchanged.
- **Do NOT add `founders` to `_DIFF_FIELDS`.** `Startup.founders` is a *relationship* to the `Founder` table (`models.py:125`), not a list of strings — `getattr` returns ORM objects and `setattr` would corrupt the relationship. The list actually lives in `raw_data["founders"]`. Handle it with an explicit `merge_list_field` union written to `raw_data`, mirroring `reviews._apply_field_updates` (`reviews.py:118-123`) and `drain_list_field_reviews.py:145-150`.
- **Delete the legacy additive-enrichment block** at `storage.py:803-811` — it is the only thing still staging list fields and it silently overrides the union.
- Extend `field_policy.norm_value` with a funding-stage normalizer. Sampling the 12 pending `funding_stage` reviews, most are pure formatting: `Series-C` → `Series C`, `Series-A2` → `Series A2`. Deliberately narrow — normalize the separator and casing only, never collapse distinct stages (`Seed` ≠ `Series A`), per Z-2's existing reasoning.

**`scripts/drain_list_field_reviews.py`** — already exists and is already correct. Re-run it to clear the 144 that the bug created.

**Regression test — the missing one.** `tests/test_storage_staging.py`: call `_diff_fields` end-to-end with an incoming record carrying new tags and founders, and assert they land in `auto_apply` and **not** in `proposed`. A pure-function test on `merge_list_field` cannot catch an unreachable branch; this is the test that would have.

---

## Phase 2 — The resolver (the autonomous part)

**New `processing/review_actions.py`** — a single reject contract. `_do_reject`'s logic (suppression row + status + `resolved_at`) is currently duplicated in five places: `reviews.py:744` (`_do_reject`), `reviews.py:506-528`, `reviews.py:1040-1058`, `adjudicate_duplicates.py:100`, `adjudicate_field_changes.py:148`. The resolver would be the sixth. Extract `reject_review(db, review)` and have all callers delegate. Approve stays in `reviews.py` — it needs `_reindex` (Qdrant + embedder) and the resolver never approves.

**New `processing/review_resolver.py`** — modelled directly on `review_explainer.py`, the established precedent for "a nightly local-model job that reads the whole pending queue under the GPU mutex."

```python
async def resolve_pending(limit: int = 120, *, apply: bool = False) -> dict
```

- Acquires `scout_controller.gpu_mutex` per item, exactly as `review_explainer.py:74` does, so it can never fight ingestion.
- **field_update**: group by `(master_id, field)` → `adjudicate_field_group` → `group_may_auto_apply`.
- **possible_duplicate**: `adjudicate_pair` → `may_auto_apply`.
- Writes `evidence["field_adjudication"]` / `evidence["adjudication"]` and `llm_explanation` on **every** review judged, whether or not it closes — so a human always sees the reasoning even when the worker declined to act.
- Auto-closes **only** the reversible direction, via `review_actions.reject_review`. `take_new` / winner picks / `same_company` / `none_fit` / anything below high confidence stays pending, untouched.
- Never raises. Degrades cleanly if Ollama is down (reuse `review_explainer.py:83`'s break-the-batch-on-first-failure logic — if the model is unreachable, stop rather than burn the window).
- `apply=False` by default, so the CLI and the first scheduled runs are report-only.

**Scheduling** — one `AsyncIOScheduler` job in `api/main.py`, the same shape as the five already there. **01:00**, ahead of the 02:00 explain job so the explainer only spends GPU on reviews the resolver left behind. Gated on `settings.resolver_enabled`.

**Throughput sanity:** local adjudication measured ~6s/call. 267 duplicate pairs ≈ 27 min; the ~111 `sub_industry` rows collapse to ~65 groups ≈ 7 min. Both fit the quiet window with room to spare.

**CLI** `scripts/resolve_reviews.py` — dry-run by default, `--apply`, `--limit`, `--type`. Follows the convention every mutating script in this repo already uses.

---

## Phase 3 — The research loop (the genuinely agentic part)

61 pending reviews (`city`, `website`, `funding_stage`, `address`, `country`, `contact_info`) cannot be settled from stored data — they need looking the company up. Phase 1's normalizer removes the formatting-only cases first; the remainder are real questions.

**New `processing/review_researcher.py`** — search → read → decide, per review:

1. Build a query from the record (reuse `regional/enrich.py:281 build_query`'s shape).
2. `ingestion.web_search.search()` — the existing Tavily → SearXNG → DuckDuckGo cascade, already throttled at 3s between requests.
3. One local 14B call judging the stored value against the proposal *and* the search results, with the same constrained-enum + `none_fit` escape hatch the field adjudicator now uses.
4. Attach findings **with source URLs** into `evidence`, matching `web_verifier`'s existing `{"web_verdict": ..., "search_results": [...]}` shape so the dashboard renders it with no UI change (`reviews.js:729` already sniffs for exactly those keys).

**Same asymmetry, applied to research:** a result that *confirms the stored value* closes the review (reversible — one suppression row). A result that favours the proposal stays pending with the evidence attached. Reuse `web_verifier._is_official_website` so an aggregator or news domain can never be adopted as a company's own site.

**Budget — the only cost control that will exist.** There is no quota accounting for Tavily anywhere in this codebase; `web_verify_chain_limit` is a hard per-run cap and is the pattern to copy. Add `resolver_max_searches: int = 40`. Exhausting it ends the run cleanly and records how many were skipped.

---

## Phase 4 — Make it safe to leave alone

This is the part that is missing today and matters most for an unattended decider.

**Durable run log.** `ScoutController` keeps run history in an in-memory `OrderedDict` (`scout_controller.py:188-200`) and the API runs under launchd `KeepAlive=true` — so any crash-restart silently erases the record of what the worker decided. A new `ResolverRun` table (`started_at`, `finished_at`, `judged`, `auto_closed`, `left_pending`, `searches_used`, `model`, `error`) follows the `MergeSnapshot`/`FieldChange` house style, with an idempotent `scripts/migrate_resolver_runs.py` migration. Every auto-close is already independently traceable through `SuppressedMatch` and `FieldChange`; this makes the *run* reconstructable too.

**Kill switch.** `resolver_enabled: bool = False` in `config/__init__.py`, defaulting **off** — same staged-rollout pattern as `ig_enabled`. Nothing runs unattended until it has been proven by hand.

**Visible on the dashboard.** A card at the top of the Review Inbox: *"Last night: judged 180, closed 96, left 84 for you"*, linking to the reviews it touched. The worker's output is worthless if nobody can see what it did.

**Heartbeat.** The owner's runbook already notes there is no alerting and the press digest is not a heartbeat. Add one line to the 08:00 press digest reporting the previous night's resolver run, so a silently dead worker is visible without anyone checking logs.

---

## Files touched

**New:** `processing/review_resolver.py` · `processing/review_actions.py` · `processing/review_researcher.py` · `scripts/resolve_reviews.py` · `scripts/migrate_resolver_runs.py` · `tests/test_review_resolver.py` · `tests/test_review_researcher.py`

**Modified:** `processing/storage.py` (`_DIFF_FIELDS`, founders union, delete legacy block) · `processing/field_policy.py` (funding-stage normalizer) · `database/models.py` (`ResolverRun`) · `config/__init__.py` (`resolver_enabled`, `resolver_max_searches`, `resolver_nightly_limit`) · `api/main.py` (01:00 job) · `api/routes/reviews.py` (delegate `_do_reject`, expose last-run summary) · `ui/static/js/views/reviews.js` + `api.js` (run card) · `tests/test_storage_staging.py` (the regression test) · `press_monitor/` (heartbeat line)

---

## Verification

1. **Phase 1 first, and measure it.** Apply the fix, then re-run the creation-rate query — `tags`/`founders` should fall to **zero new reviews** on the next sweep. Re-run `drain_list_field_reviews.py` (dry-run, then `--apply`) and confirm the 144 close.
2. **Unit** — the `_diff_fields` end-to-end regression test; a multi-candidate test asserting two pending reviews with *different* values for one `(master, field)` are never auto-applied (the 12 Aug bug, must not be reintroducible); `reject_review` parity tests proving the extracted contract matches `_do_reject`'s current behaviour.
3. **Resolver report-only run** — `python3 scripts/resolve_reviews.py --limit 50`, output reviewed **with the owner** before `--apply` is ever used. This is the same gate the adjudicators went through, and it caught a real prompt bug both times.
4. **Spot-check, not aggregate counts.** After the first `--apply`, pick individual closed reviews and compare the live field values against their evidence trail. Matching totals is *not* sufficient — that is precisely what hid the 130-field clobber in August.
5. **Ollama-down path** — stop Ollama, run the resolver, confirm it exits cleanly, records the failure in `ResolverRun`, and closes nothing.
6. **Budget** — confirm a full research run stops at `resolver_max_searches` and reports the number skipped.
7. **Restart durability** — trigger a run, restart the API (`launchctl kickstart -k`), confirm the run record survives.
8. **Full suite** — `python3 -m pytest -q` green (467 at time of writing) · `curl /health` OK · Review Inbox loads with the new card and the Playwright smoke test (`tests/test_dashboard_smoke.py`) still passes.

---

## Explicitly out of scope

- **ElizaOS / OpenClaw runtime, and any Discord surface** — owner chose the Python worker. `openclaw/*.json` stay config-only.
- **Auto-applying overwrites or merges.** `take_new`, winner picks, and `same_company` remain human decisions at every confidence. Revisit only after the resolver has a proven track record — the undo machinery (`MergeSnapshot`, `FieldChange`) now exists to make that a real option later.
- **Cloud models.** All inference stays local; `anthropic_api_key` remains benchmark-scripts-only.
- **Rewriting the matcher or dedup thresholds.** This plan resolves what the matcher stages; it does not change what gets staged (beyond Phase 1's bug fix).
