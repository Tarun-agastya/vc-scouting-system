"""
Claude vs the local model, on the REAL extraction prompt — dry run by default.

Answers one question before any migration is attempted: does a cloud model
actually extract better than `qwen2.5:7b-instruct` on this project's own
prompt, and at what token cost?

Why this exists
---------------
The Qwen A/B on 17 Sep (validation/qwen_model_ab_2026-09.md) established the
local baseline on five hand-labelled cases:

    qwen2.5:7b-instruct   precision 1.00   recall 0.70   F1 0.82

and the measurement that motivates moving at all: a sweep on 21 Sep took
7h44m for 2,490 serialised calls, because one GPU runs one call at a time.

The number that decides this is **precision, not recall**. The local model
already scores 1.00 there, and the failures this database actually suffers are
false positives — 16 university course titles were stored as startups in
September alone. A cloud model that finds more companies while also inventing
a few is a regression wearing a better score.

Fair-comparison guarantees
--------------------------
* Uses the production prompt verbatim — SYSTEM_EXTRACTOR, EXTRACTION_PROMPT
  and the include/exclude rules from config/tuning.yaml, via the same loader
  `qwen_client.extract_startups` uses. If you edit the prompt, this follows.
* Uses the production JSON schema (`_STARTUP_EXTRACTION_SCHEMA`), converted to
  a tool definition, which is the Messages API's analogue of Ollama's
  `format=` constrained decoding.
* Scores against the same five labelled cases as the Qwen run, including two
  precision traps where the correct answer is "one company" and "none at all".

One deliberate difference: production puts the whole prompt in the user
message, while this puts the fixed 5,230-char rules block in a cached `system`
block and sends only the chunk text as the user turn. That is not a thumb on
the scale — `{text}` is the last thing in the template, so the fixed part is a
clean prefix, and this is the layout production should adopt precisely because
it is cacheable. The run reports cache hits so you can see it working.

Safety
------
* **Dry run by default.** Prints the exact payload shape and token estimate
  and spends nothing. `--run` is required to make a single paid call.
* Hard `--max-calls` ceiling (default 20), enforced before each request, the
  same per-run-cap pattern as `web_verify_chain_limit`.
* Touches no database. Imports nothing from `processing/` or `database/`.
* Reads the key from the environment only, never prints it, and refuses to
  run if it is absent.

Usage
-----
    python3 scripts/bench_cloud_models.py                      # dry run, free
    export ANTHROPIC_API_KEY=...                               # never committed
    python3 scripts/bench_cloud_models.py --run
    python3 scripts/bench_cloud_models.py --run \
        --models claude-haiku-4-5-20251001,claude-sonnet-5 \
        --price-in 1 --price-out 5 --price-cache-read 0.1      # €/M tokens
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

DEFAULT_MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-5"]

# The local baseline this is measured against — from the 17 Sep A/B.
LOCAL_BASELINE = {"model": "qwen2.5:7b-instruct (local)",
                  "precision": 1.00, "recall": 0.70, "f1": 0.82, "mean_s": 64.8}

# Identical to the Qwen A/B's set, so the two runs are directly comparable.
CASES = [
    ("de_funding",
     """Das Münchner Startup Voltaro hat in einer Seed-Runde 4,2 Millionen Euro
     eingesammelt. Angeführt wurde die Runde von High-Tech Gründerfonds. Voltaro entwickelt
     Batteriespeicher für Gewerbeimmobilien. Ebenfalls frisches Kapital erhielt das Augsburger
     Unternehmen Klaerwerk GmbH, das Sensorik für die Abwasseraufbereitung baut.""",
     {"voltaro", "klaerwerk"}),

    ("en_batch",
     """The latest cohort includes three companies: Nimbus Foundry, which builds
     additive manufacturing tooling for aerospace; Cellulo Bio, a biotech developing enzymatic
     plastic recycling; and Hafen Robotics, an autonomous port-logistics startup from Hamburg.""",
     {"nimbus foundry", "cellulo bio", "hafen robotics"}),

    # The logo_grid case: bare names, no descriptions. Historically where local
    # recall collapsed and facts bled between neighbours.
    ("bare_names",
     """Portfolio\nAquilo\nBrightMesh\nCarbonCircle\nDeltaSense\n""",
     {"aquilo", "brightmesh", "carboncircle", "deltasense"}),

    # Precision trap 1: one real startup among an institution and an incumbent.
    ("precision_mixed",
     """Bei der Preisverleihung der IHK Schwaben wurde das Startup Thermolux
     ausgezeichnet, das Infrarot-Heizsysteme entwickelt. Anwesend waren auch Vertreter der
     Sparkasse Augsburg und der Liebherr-Werke.""",
     {"thermolux"}),

    # Precision trap 2: the real text that produced 16 junk records in September.
    # Anything returned here is a false positive.
    ("precision_none",
     """Konstruktion und Entwerfen\nGeschichte und Theorie der Architektur\n
     Baukonstruktion und Entwerfen\nEnergierecht\nBilanzierung und betriebliche Steuerlehre""",
     set()),
]


def _norm(n: str) -> str:
    return (n or "").strip().lower().replace(" gmbh", "").replace(" ag", "").strip()


def build_prompt_parts(chunk: str):
    """
    Return (cached_prefix, user_text) using the production prompt verbatim.

    `{text}` is the final token of EXTRACTION_PROMPT, so everything before it
    is a stable prefix that is byte-identical on every call — which is the
    whole reason caching pays here.
    """
    from reasoning.prompts import EXTRACTION_PROMPT, SYSTEM_EXTRACTOR
    from config.tuning_loader import get_extraction_rules

    rules = get_extraction_rules()
    exclude_rules = "\n".join(f"- {line}" for line in (rules.get("exclude") or []))
    filled = EXTRACTION_PROMPT.format(
        text="\x00PLACEHOLDER\x00",
        include_rules=rules.get("include", ""),
        exclude_rules=exclude_rules,
    )
    prefix, _, suffix = filled.partition("\x00PLACEHOLDER\x00")
    if suffix.strip():
        # Template changed and text is no longer last — caching still works on
        # the prefix, but say so rather than silently measuring something else.
        print(f"  ! note: {len(suffix)} chars follow the chunk text in the template", file=sys.stderr)
    return SYSTEM_EXTRACTOR + "\n\n" + prefix, chunk + suffix


def build_tool():
    """Production's JSON schema, expressed as a tool the model must call."""
    from reasoning.qwen_client import _STARTUP_EXTRACTION_SCHEMA
    return {
        "name": "record_startups",
        "description": "Record every startup company found in the text.",
        "input_schema": _STARTUP_EXTRACTION_SCHEMA,
    }


def call_model(model: str, cached_prefix: str, user_text: str, tool: dict, api_key: str):
    import httpx

    payload = {
        "model": model,
        "max_tokens": 3000,
        "temperature": 0,
        "system": [{"type": "text", "text": cached_prefix,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    t0 = time.time()
    with httpx.Client(timeout=120) as client:
        r = client.post(API_URL, json=payload, headers=headers)
    elapsed = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    names = []
    for block in body.get("content", []):
        if block.get("type") == "tool_use":
            names = [s.get("name") for s in (block.get("input") or {}).get("startups", [])]
    return names, body.get("usage", {}), elapsed


def score(model_label: str, rows):
    tp = fp = fn = 0
    for _, expected, got in rows:
        names = {_norm(n) for n in got if n}
        hit = {n for n in names if any(e in n or n in e for e in expected)} if expected else set()
        miss = {e for e in expected if not any(e in n or n in e for n in names)}
        extra = {n for n in names if not any(e in n or n in e for e in expected)}
        tp += len(hit); fp += len(extra); fn += len(miss)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"model": model_label, "precision": prec, "recall": rec, "f1": f1,
            "tp": tp, "fp": fp, "fn": fn}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--run", action="store_true",
                    help="actually call the API (default is a free dry run)")
    ap.add_argument("--max-calls", type=int, default=20,
                    help="hard ceiling on paid requests for this run")
    ap.add_argument("--price-in", type=float, default=None, help="€ per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="€ per 1M output tokens")
    ap.add_argument("--price-cache-read", type=float, default=None,
                    help="€ per 1M cached-read input tokens")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tool = build_tool()
    prefix, _ = build_prompt_parts("")

    print(f"Cases            : {len(CASES)}")
    print(f"Models           : {', '.join(models)}")
    print(f"Cached prefix    : {len(prefix)} chars (~{len(prefix)//4} tokens), identical every call")
    print(f"Planned calls    : {len(CASES) * len(models)}  (ceiling {args.max_calls})")
    print(f"Local baseline   : precision {LOCAL_BASELINE['precision']:.2f}  "
          f"recall {LOCAL_BASELINE['recall']:.2f}  {LOCAL_BASELINE['mean_s']:.0f}s/call")

    if not args.run:
        print("\nDRY RUN — nothing was sent and nothing was charged.")
        print("Set ANTHROPIC_API_KEY and re-run with --run to measure for real.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\nANTHROPIC_API_KEY is not set. Refusing to run.", file=sys.stderr)
        sys.exit(2)

    planned = len(CASES) * len(models)
    if planned > args.max_calls:
        print(f"\n{planned} calls exceeds --max-calls {args.max_calls}. Refusing.", file=sys.stderr)
        sys.exit(2)

    results, calls = [], 0
    for model in models:
        print(f"\n######## {model}")
        rows, tok_in, tok_out, tok_cache_r, tok_cache_w, total_t = [], 0, 0, 0, 0, 0.0
        for label, text, expected in CASES:
            if calls >= args.max_calls:
                print("  call ceiling reached — stopping"); break
            cached_prefix, user_text = build_prompt_parts(text)
            try:
                names, usage, el = call_model(model, cached_prefix, user_text, tool, api_key)
                calls += 1
            except Exception as exc:
                print(f"    {label:16} ERROR {type(exc).__name__}: {str(exc)[:110]}")
                rows.append((label, expected, [])); continue
            total_t += el
            tok_in += usage.get("input_tokens", 0)
            tok_out += usage.get("output_tokens", 0)
            tok_cache_r += usage.get("cache_read_input_tokens", 0)
            tok_cache_w += usage.get("cache_creation_input_tokens", 0)
            rows.append((label, expected, names))
            got = sorted(_norm(n) for n in names if n)
            print(f"    {label:16} {el:5.1f}s  got={got}")
            miss = sorted(e for e in expected if not any(e in n or n in e for n in got))
            extra = sorted(n for n in got if not any(e in n or n in e for e in expected))
            if miss:  print(f"                     MISSED   {miss}")
            if extra: print(f"                     FALSE +  {extra}")

        r = score(model, rows)
        r.update(mean_s=total_t / max(len(rows), 1), tok_in=tok_in, tok_out=tok_out,
                 tok_cache_read=tok_cache_r, tok_cache_write=tok_cache_w)
        results.append(r)

    print("\n" + "=" * 86)
    print(f"{'model':34} {'prec':>6} {'recall':>7} {'F1':>6} {'FP':>4} {'FN':>4} {'s/call':>8}")
    print("-" * 86)
    b = LOCAL_BASELINE
    print(f"{b['model']:34} {b['precision']:6.2f} {b['recall']:7.2f} {b['f1']:6.2f} "
          f"{'-':>4} {'-':>4} {b['mean_s']:7.1f}s")
    for r in results:
        print(f"{r['model']:34} {r['precision']:6.2f} {r['recall']:7.2f} {r['f1']:6.2f} "
              f"{r['fp']:4} {r['fn']:4} {r['mean_s']:7.1f}s")
    print("=" * 86)

    print("\nTokens (the cache columns are the cost story — the prefix repeats every call):")
    for r in results:
        print(f"  {r['model']:34} in={r['tok_in']:>7}  out={r['tok_out']:>6}  "
              f"cache_write={r['tok_cache_write']:>7}  cache_read={r['tok_cache_read']:>7}")
        if args.price_in and args.price_out:
            cr = args.price_cache_read if args.price_cache_read is not None else args.price_in
            cost = (r["tok_in"] * args.price_in + r["tok_out"] * args.price_out
                    + r["tok_cache_write"] * args.price_in
                    + r["tok_cache_read"] * cr) / 1_000_000
            per_month = cost / max(len(CASES), 1) * 20_000
            print(f"  {'':34} this run ~€{cost:.4f}   extrapolated to 20k calls/month ~€{per_month:,.0f}")
    if not (args.price_in and args.price_out):
        print("\n  Pass --price-in/--price-out/--price-cache-read (€ per 1M tokens, from current")
        print("  published pricing) to turn the token counts above into a monthly figure.")

    print("\nRead precision first. The local model scores 1.00 there; a model that raises")
    print("recall while dropping precision is a regression, not an upgrade.")


if __name__ == "__main__":
    main()
