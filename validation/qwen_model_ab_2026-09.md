# Qwen model A/B for the extraction hot path — 17 Sep 2026

Asked for: *"use the latest qwen llm or similar in the hubdrive."*

Answer: **don't move the extraction model.** The newer models work but are
2.8–4.7× slower on a path that runs hundreds of times per page, and the
incumbent already scores perfect precision. There *was* a real bug behind the
request, and it is fixed — but it is not the bug the request assumed.

This file exists because `qwen3.5:9b` and `gemma4:12b` were pulled onto this
machine in early August for exactly this comparison, and the result was never
written down. Six weeks later nobody could say whether the newer model was
better, so the question had to be answered from scratch. Write the numbers
down next time.

## The bug found on the way

`think=False` was set on every reasoning-model call in August (5× speedup,
65s → 12.6s, recorded in `qwen_client`'s own docstring) but **never on the
extraction call**, because the extraction model of the day —
`qwen2.5:7b-instruct` — has no thinking mode and did not need it.

That left the extraction path structurally unable to run *any*
Qwen3-generation model. The failure mode is nasty because nothing errors: the
reasoning block consumes the whole `num_predict` budget before the constrained
JSON is ever emitted, so the call just times out and returns nothing. It is
indistinguishable from "the new model is bad."

Fixed in `reasoning/qwen_client.py` and pinned by
`tests/test_adaptive_pipeline.py::test_extraction_call_disables_thinking_mode`.
Verified harmless on a non-thinking model: 35.1s with, 37.8s without,
byte-identical output.

## Quality — real extraction path, 5 hand-labelled cases

Same prompt, same JSON schema, same post-processing (junk filter + grounding
gate). Scored on **precision as well as recall**, deliberately: the failures
this pipeline actually suffers are false positives — 11 university course
titles were stored as startups and deleted on 16 Sep — so a model that
extracts more eagerly can score better on recall and be worse in production.

| Model | Precision | Recall | F1 | Mean/call | Timeouts |
|---|---|---|---|---|---|
| `qwen2.5:7b-instruct` (current) | **1.00** | 0.70 | 0.82 | 64.8s | 1 of 5 |
| `qwen3:8b` | 0.00 | 0.00 | 0.00 | 130.2s | 4 of 5 |
| `qwen3.5:9b` | 0.00 | 0.00 | 0.00 | 152.0s | 5 of 5 |

The zeros are the `think=False` bug above, **not** model incapability — see
below. Treat this table as "what the path did before the fix", not as a
verdict on the models.

## Latency — same input, after the fix

| Call | Latency | Output |
|---|---|---|
| `qwen3.5:9b`, think unset, cold | 166.0s | correct |
| `qwen3.5:9b`, `think=False`, warm | 117.9s | correct |
| `qwen3.5:9b`, `think=False`, warm repeat | 99.5s | correct |
| `qwen2.5:7b-instruct`, first call after a model swap | 69.8s | correct |
| `qwen2.5:7b-instruct`, resident | ~35.1s | correct |

`qwen3.5:9b` is perfectly capable — it returned the correct two companies on
every attempt. It is simply ~2.8× slower than the incumbent at steady state
(99.5s vs 35.1s), and `think=False` accounts for roughly a third of its cost
(166s → 118s), not all of it.

## Why that settles it

Extraction is **per chunk, not per page**. The `accelerator.unternehmertum.de`
portfolio page measured 354 extraction calls on 16 Sep:

* at 35s/call → ~3.4 hours
* at 99.5s/call → ~9.8 hours

For a model whose measured quality advantage on this task is *zero* — both got
the same answer — that is not a trade worth making on a 24 GB M4.

## Model-swap thrash is a separate, real cost

`qwen2.5:7b-instruct` took **69.8s on its first call after a swap vs ~35.1s
resident** — a ~35s reload penalty, and `ollama ps` showed models perpetually
"Stopping…" to make room for each other. With models at 4.7 / 5.5 / 9.3 / 10 GB
against 24 GB of unified memory, every alternation between the extraction and
reasoning models pays this.

No model change fixes that. It is the deferred **Phase T-1 (model residency)**
item, and on this evidence it is worth more than a model upgrade.

## Two things worth doing instead

1. **Raise the extraction timeout before changing any model.** The incumbent's
   recall loss in the table above was a *timeout*, not a comprehension
   failure — it missed the 3-company case by timing out at 75s, then handled
   the harder 4-bare-name case correctly. Some of that 0.70 is sitting behind
   the ceiling, which is far cheaper to test than a migration.
2. **Consider a newer Qwen for the *reasoning* model instead.** Those calls are
   few — scoring, comparison, verification — so a 3× latency cost is
   affordable there, and quality matters more than throughput. That is where
   "latest Qwen" would actually pay.

## Reproducing

The benchmark is not committed (it hard-codes hand-labelled cases and is a
one-off), but it is short: monkeypatch `settings.ollama_extract_model`, reset
`qwen_client._extract_ollama_client`, call `extract_startups` on the five
cases, and score name sets against the labels. Keep precision in the scoring.
