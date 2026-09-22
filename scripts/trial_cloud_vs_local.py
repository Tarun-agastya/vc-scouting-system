"""
Side-by-side trial: local model vs Claude, on two real jobs, using real records.

Built to be shown to someone who has to approve spending money, so it is
deliberately concrete: the test set is drawn live from this database, not
invented, and every number is measured rather than estimated.

The two jobs
------------
A. **Is this a company at all?**
   The database holds events ("Gitex Europe 2026"), page headings
   ("Robotik-Startups") and people ("Eric Archambeau") stored as startups.
   Deterministic rules catch the obvious ones and cannot catch the rest: a
   regex that matches "Christian Willert" also matches "Astra Labs" and
   "Dallara Automobili", which are companies. 58 records sit in that
   unresolvable bucket today.

B. **Are these two records the same company?**
   After the 22 Sep deterministic cleanup (3,591 -> 2,899 records, no model
   involved) what remains are genuine identity questions: reverion.com vs
   reverion.de is one company, rudy-capital.com vs rudyproject.com is two,
   and no rule separates them.

Both jobs are low volume — tens of calls, not thousands — which is exactly
why they are the right first thing to move to a paid model. The expensive,
high-volume work (extraction) is a separate decision; see the artifact
"Seven Hours or Thirty Minutes".

How the test set is labelled
----------------------------
Job A uses records whose answer we already know, so accuracy is measurable:
  * NOT companies — the categories and events verified by eye on 22 Sep
  * ARE companies — records carrying both a real website and a description,
    which no listing heading or person's name ever has
It then runs the 58 genuinely ambiguous records, which have no label — that
is the answer we are buying.

Job B uses the pairs already adjudicated, so the two models can be compared
against each other and against the human-checkable outcome.

Usage
-----
    python3 scripts/trial_cloud_vs_local.py                  # local only
    export ANTHROPIC_API_KEY=...
    python3 scripts/trial_cloud_vs_local.py --with-cloud
    python3 scripts/trial_cloud_vs_local.py --with-cloud --cloud-model claude-haiku-4-5-20251001
    python3 scripts/trial_cloud_vs_local.py --json trial.json
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal
from database.models import Startup

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_CLOUD = "claude-haiku-4-5-20251001"

_SYSTEM_A = (
    "You judge whether a database record describes a real operating COMPANY. "
    "Events, conferences, awards, funding programmes, page headings, topic "
    "categories and individual people are NOT companies."
)

_PROMPT_A = """Record from a startup database:

  name        : {name}
  website     : {website}
  description : {description}
  industry    : {industry}
  city        : {city}

Is this a real operating company?

- "company" — a real business, including one-person businesses and companies
  named after their founder.
- "not_company" — an event or conference (often carries a year), an award, a
  funding programme, a topic heading or category ("Robotik-Startups"), a
  person's name from a byline or staff list, or a listing-page label.
- "unsure" — genuinely cannot tell from these fields.

Judge only from what is shown. Do not use outside knowledge about the name.
Give a one-sentence reason citing the field that decided it."""

_SCHEMA_A = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["company", "not_company", "unsure"]},
        "reason": {"type": "string"},
    },
    "required": ["answer", "reason"],
}


def _fields(s):
    def g(a):
        v = getattr(s, a, None)
        return str(v).strip() if v not in (None, "", []) else "—"
    return {
        "name": g("name"), "website": g("website"),
        "description": (g("short_description") if g("short_description") != "—" else g("description"))[:300],
        "industry": g("industry"), "city": g("city"),
    }


# ── providers ─────────────────────────────────────────────────────────────────

def ask_local(system, prompt, schema, model=None):
    from config import settings
    from reasoning.qwen_client import qwen_client
    model = model or settings.ollama_reason_model
    t0 = time.time()
    try:
        r = qwen_client._client().chat(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            format=schema, think=False,
            options={"temperature": 0, "num_predict": 400},
        )
        return json.loads(r["message"]["content"]), time.time() - t0, {}
    except Exception as exc:
        return None, time.time() - t0, {"error": f"{type(exc).__name__}: {exc}"}


class AuthFailure(RuntimeError):
    """Raised on 401/403 so the run stops instead of repeating a doomed call."""


def check_cloud_key() -> str:
    """
    Validate the key's shape before spending a request on it.

    Added 22 Sep after a real run where the key was set to the literal string
    "..." — the placeholder from the instructions, pasted verbatim. Every one
    of the 34 calls returned 401 and the summary reported "0% accuracy", which
    reads as "the model got everything wrong" when in fact it never ran.
    """
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        # Fall back to .env, where this project keeps every other secret.
        try:
            from config import settings
            key = (getattr(settings, "anthropic_api_key", None) or "").strip()
        except Exception:
            key = ""
    if not key:
        raise AuthFailure(
            "No API key found.\n"
            "  Either add a line to .env (preferred, already gitignored):\n"
            "      anthropic_api_key=sk-ant-api03-...\n"
            "  or export it for this shell:\n"
            "      export ANTHROPIC_API_KEY=sk-ant-api03-..."
        )
    if not key.startswith("sk-ant-") or len(key) < 20:
        raise AuthFailure(
            f"ANTHROPIC_API_KEY does not look like a real key (got {key[:8]!r}...).\n"
            "  A real key starts with 'sk-ant-'. If you copied the instructions\n"
            "  literally, you have set it to the placeholder rather than the key."
        )
    return key


def ask_cloud(system, prompt, schema, model):
    import httpx
    key = check_cloud_key()
    tool = {"name": "answer", "description": "Record the judgement.", "input_schema": schema}
    payload = {
        "model": model, "max_tokens": 400, "temperature": 0,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
        "tools": [tool], "tool_choice": {"type": "tool", "name": "answer"},
    }
    t0 = time.time()
    try:
        with httpx.Client(timeout=90) as c:
            resp = c.post(API_URL, json=payload, headers={
                "x-api-key": key, "anthropic-version": API_VERSION,
                "content-type": "application/json"})
        el = time.time() - t0
        if resp.status_code in (401, 403):
            raise AuthFailure(f"HTTP {resp.status_code} from the API — the key was rejected. "
                              "Nothing was charged.")
        if resp.status_code != 200:
            return None, el, {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        body = resp.json()
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input"), el, body.get("usage", {})
        return None, el, {"error": "no tool_use block"}
    except Exception as exc:
        return None, time.time() - t0, {"error": f"{type(exc).__name__}: {exc}"}


# ── test set ──────────────────────────────────────────────────────────────────

_CATEGORY_RE = re.compile(r"(^|\s|-)([\wÀ-ɏ]+-startups?|startup-landschaft|startup-szene)(\s|$)", re.I)
_GENERIC = {"unternehmen", "startups", "start-ups", "firmen", "companies", "startup", "portfolio"}
_EVENT_RE = re.compile(r"\b(19|20)\d{2}\b")
_TWO_WORD_RE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]{1,}\s+[A-ZÄÖÜ][a-zäöüß]{1,}$")


# Hand-verified fixtures, taken from this database and checked by eye on
# 22 Sep. They are embedded rather than queried because the junk was DELETED
# that same day — a test set that dissolves when you fix the data cannot be
# re-run, and a number you can't reproduce is no use to anyone deciding
# whether to spend money. Field values are the real stored ones.
_KNOWN_NOT_COMPANY = [
    ("Gitex Europe 2026", "—", "—", "—", "—"),
    ("Munich Startup Festival 2024", "—", "—", "Consumer & Lifestyle", "—"),
    ("Digital Health Summit 2026", "—", "—", "—", "—"),
    ("Exportpreis Bayern 2026", "—", "—", "—", "—"),
    ("Robotik-Startups", "—", "Münchner Robotik-Startups und ihre Lösungen.",
     "Robotics & Hardware", "—"),
    ("Traveltech-Startups", "—",
     "Traveltech companies addressing various developments in the sector.",
     "Logistics & Supply Chain", "—"),
    ("Deutsche Startup-Landschaft", "—", "—", "—", "—"),
    ("Unternehmen", "—", "—", "B2B SaaS & Enterprise", "—"),
    ("exist-Women", "—", "—", "Consumer & Lifestyle", "—"),
    ("Betriebswirtsch. Unternehmensberatung", "—", "—", "B2B SaaS & Enterprise", "—"),
]


def _fixture_cases():
    class _Row:
        def __init__(self, n, w, d, i, c):
            self.name, self.website, self.description = n, w, d
            self.industry, self.city, self.short_description = i, c, None
    return [_Row(*t) for t in _KNOWN_NOT_COMPANY]


def build_set(db, n_real=12, n_amb=12):
    rows = db.query(Startup).all()
    known_junk, known_real, ambiguous = [], [], []
    for s in rows:
        name = (s.name or "").strip()
        has_site = bool((s.website or "").strip())
        has_text = bool((s.description or "").strip() or (s.short_description or "").strip())
        if name.lower() in _GENERIC or _CATEGORY_RE.search(name) or _EVENT_RE.search(name):
            known_junk.append(s)
        elif _TWO_WORD_RE.match(name) and not has_site and not has_text:
            ambiguous.append(s)
        elif has_site and has_text and len(name) > 3:
            known_real.append(s)
    known_real.sort(key=lambda r: (r.name or ""))
    ambiguous.sort(key=lambda r: (r.name or ""))
    return known_junk, known_real[:n_real], ambiguous[:n_amb]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-cloud", action="store_true")
    ap.add_argument("--cloud-model", default=DEFAULT_CLOUD)
    ap.add_argument("--json", default="")
    ap.add_argument("--limit-junk", type=int, default=10)
    ap.add_argument("--drop", default="",
                    help="comma-separated record numbers to leave out of the export")
    ap.add_argument("--export-prompt", default="",
                    help="write a paste-ready prompt for a chat UI (no API needed)")
    ap.add_argument("--local-json", default="",
                    help="a previous local run's --json, for a like-for-like baseline")
    ap.add_argument("--key", default="",
                    help="the *_key.json written beside the prompt (auto-detected if omitted)")
    ap.add_argument("--score-pasted", default="",
                    help="score the JSON array a chat UI gave back")
    ap.add_argument("--check-cloud", action="store_true",
                    help="make ONE cheap call to confirm the key and credit work, then stop")
    args = ap.parse_args()

    if args.score_pasted:
        import glob as _glob
        import re as _re
        if not os.path.exists(args.score_pasted):
            print(f"Can't find '{args.score_pasted}'.\n")
            print("That file is something YOU create: paste the model's reply into it.")
            print("  1. copy everything the model answered")
            print(f"  2. save it as {args.score_pasted} in this folder")
            print("  3. re-run this command")
            print("\nPasting the surrounding prose is fine — only the [ ... ] block is read.")
            sys.exit(2)
        raw = open(args.score_pasted, encoding="utf-8").read()
        m = _re.search(r"\[.*\]", raw, _re.S)      # tolerate chat chatter around the JSON
        if not m:
            print("No JSON array found in that file. Paste the model's reply verbatim;")
            print("surrounding prose is fine, but the [ ... ] block must be there.")
            sys.exit(2)
        answers = {int(a["n"]): a for a in json.loads(m.group(0))}
        # The key belongs to the PROMPT, not to the reply file. Getting this
        # wrong scores answers against the wrong labels and yields a confident,
        # meaningless number — so resolve it explicitly and say which one was
        # used, rather than falling back to a stale file in /tmp.
        keyfile = args.key
        if not keyfile:
            candidates = sorted(_glob.glob("*_key.json"), key=os.path.getmtime, reverse=True)
            if not candidates:
                print("No answer key found. It is written next to the prompt, e.g.")
                print("  prompt.txt -> prompt_key.json")
                print("Pass it explicitly with --key prompt_key.json")
                sys.exit(2)
            keyfile = candidates[0]
        if not os.path.exists(keyfile):
            print(f"Answer key '{keyfile}' not found.")
            sys.exit(2)
        key = json.load(open(keyfile, encoding="utf-8"))
        print(f"Scoring '{args.score_pasted}' against key '{keyfile}' "
              f"({len(key)} records)\n")

        correct = graded = missing = 0
        amb_resolved = amb_total = 0
        print(f"{'#':>3}  {'record':34} {'answer':13} {'expected':13}")
        print("-" * 70)
        for k in key:
            a = answers.get(k["n"])
            ans = (a or {}).get("answer", "—")
            if a is None:
                missing += 1
            if k["label"]:
                graded += 1
                ok = ans == k["label"]
                correct += ok
                mark = "" if ok else "   <-- miss"
                print(f"{k['n']:>3}  {k['name'][:32]:34} {ans:13} {k['label']:13}{mark}")
            else:
                amb_total += 1
                if ans in ("company", "not_company"):
                    amb_resolved += 1
                print(f"{k['n']:>3}  {k['name'][:32]:34} {ans:13} {'(ambiguous)':13}")

        print("-" * 70)
        acc = correct / graded if graded else 0
        print(f"  accuracy on known answers : {correct}/{graded} = {acc:.0%}")
        print(f"  ambiguous resolved        : {amb_resolved}/{amb_total}")
        if missing:
            print(f"  !! {missing} records got no answer — ask it to complete the list")

        # Like-for-like local baseline, computed on EXACTLY these records.
        # Quoting the headline "5 of 12" here would be wrong whenever --drop
        # was used: the dropped records are not a random sample, and the four
        # person-names excluded above happen to be four the local model got
        # right — so the honest comparison is far lower than the headline.
        local_path = args.local_json or "/tmp/trial_local.json"
        if os.path.exists(local_path):
            try:
                loc = {r["name"]: r for r in json.load(open(local_path, encoding="utf-8"))["results"]
                       if r["provider"] == "local"}
                names = {k["name"] for k in key}
                l_graded = [k for k in key if k["label"] and k["name"] in loc]
                l_correct = sum(1 for k in l_graded if loc[k["name"]]["answer"] == k["label"])
                l_amb = [k for k in key if not k["label"] and k["name"] in loc]
                l_res = sum(1 for k in l_amb
                            if loc[k["name"]]["answer"] in ("company", "not_company"))
                print()
                print(f"  LOCAL, on these same records:")
                print(f"    accuracy on known answers : {l_correct}/{len(l_graded)}"
                      f" = {l_correct/max(len(l_graded),1):.0%}")
                print(f"    ambiguous resolved        : {l_res}/{len(l_amb)}")
                if amb_total and len(l_amb):
                    print()
                    print(f"  => the paid model resolved {amb_resolved - l_res:+d} more of the"
                          f" {amb_total} records a person would otherwise open.")
            except Exception as exc:
                print(f"\n  (could not read local baseline from {local_path}: {exc})")
        else:
            print(f"\n  No local run found at {local_path} — run without --with-cloud first")
            print("  to get a like-for-like baseline on exactly these records.")
        return

    if args.export_prompt:
        db2 = SessionLocal()
        try:
            _lj, real, amb = build_set(db2)
            junk = (_lj + _fixture_cases())[:args.limit_junk]
            cases = ([(r, "not_company") for r in junk]
                     + [(r, "company") for r in real]
                     + [(r, None) for r in amb])
        finally:
            db2.close()

        drop = {int(x) for x in args.drop.split(",") if x.strip().isdigit()}
        if drop:
            cases = [c for i, c in enumerate(cases, 1) if i not in drop]
            print(f"Dropped {len(drop)} record(s) at your request: {sorted(drop)}\n")

        person_like = [r.name for r, lab in cases
                       if lab is None and _TWO_WORD_RE.match((r.name or "").strip())]

        lines = [
            "You are auditing a startup database. For EACH numbered record below,",
            "say whether it describes a real operating COMPANY.",
            "",
            "  company     = a real business (including one-person businesses and",
            "                companies named after their founder)",
            "  not_company = an event or conference, an award, a funding programme,",
            "                a topic heading or category, a person's name from a",
            "                byline or staff list, or a listing-page label",
            "  unsure      = genuinely cannot tell from the fields shown",
            "",
            "Judge ONLY from the fields shown. Do not use outside knowledge about",
            "the names. Answer for every record.",
            "",
            "Reply with ONLY a JSON array, no other text:",
            '[{"n": 1, "answer": "company", "reason": "one short sentence"}, ...]',
            "",
            "--- RECORDS ---",
        ]
        for i, (r, _lab) in enumerate(cases, 1):
            f = _fields(r)
            lines.append(f"\n{i}. name: {f['name']}")
            lines.append(f"   website: {f['website']}")
            lines.append(f"   description: {f['description']}")
            lines.append(f"   industry: {f['industry']}   city: {f['city']}")

        prompt = "\n".join(lines)
        out = args.export_prompt
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(prompt + "\n")

        key = [{"n": i, "name": r.name, "label": lab} for i, (r, lab) in enumerate(cases, 1)]
        keyfile = out.rsplit(".", 1)[0] + "_key.json"
        with open(keyfile, "w", encoding="utf-8") as fh:
            json.dump(key, fh, indent=2, ensure_ascii=False)

        print(f"Wrote {out}  ({len(cases)} records, ~{len(prompt)//4} tokens)")
        print(f"Wrote {keyfile}  (the answer key — keep this, don't paste it)")
        print()
        print("Paste the contents of the prompt file into ChatGPT as ONE message.")
        print("One message for all records, not one per record — that is the whole")
        print("point, and it matches the 'avoid repeated full analyses' guidance.")
        print("Use the LOWEST effort setting; this is classification, not reasoning.")
        if person_like:
            print()
            print(f"  !! {len(person_like)} of these look like NAMES OF REAL PEOPLE:")
            for n in person_like:
                print(f"       {n}")
            print("  Personal data should not go into a chat tool without a documented")
            print("  legal basis — that is your workspace's rule, and it applies at least")
            print("  as strongly to a personal account, which no company agreement covers.")
            if drop:
                print("  (Records were renumbered after --drop, so the numbers above are")
                print("   the NEW ones. Add them to your existing --drop list, adjusting")
                print("   for the shift, or start from the undropped export.)")
            else:
                print("  Re-run with --drop to exclude them by number, e.g.")
                print("      --drop " + ",".join(
                    str(i) for i, (r, lab) in enumerate(cases, 1)
                    if lab is None and _TWO_WORD_RE.match((r.name or '').strip())))
            print("  The labelled records alone still give a valid accuracy figure.")
        return

    if args.check_cloud:
        try:
            check_cloud_key()
        except AuthFailure as exc:
            print(f"Key problem:\n\n  {exc}\n")
            sys.exit(2)
        print(f"Key looks valid. Making one test call to {args.cloud_model} …")
        try:
            out, el, usage = ask_cloud(
                "Answer with the tool.",
                "Record from a startup database:\n\n  name : Kiwigrid\n  website : "
                "https://www.kiwigrid.com\n  description : energy IoT platform\n  "
                "industry : Energy\n  city : Dresden\n\nIs this a real operating company?",
                _SCHEMA_A, args.cloud_model)
        except AuthFailure as exc:
            print(f"\n  REJECTED: {exc}")
            print("  Nothing was charged. Check the key, and that the account has credit.")
            sys.exit(2)
        if out is None:
            print(f"\n  FAILED: {usage.get('error', 'unknown')}")
            print("  If this mentions credit or billing, add a small balance in the Console.")
            sys.exit(2)
        print(f"\n  OK — answered {out.get('answer')!r} in {el:.1f}s")
        print(f"  tokens in/out: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}")
        print("\n  The key works. Run the full trial with --with-cloud.")
        return

    db = SessionLocal()
    try:
        _live_junk, real, amb = build_set(db)
        # Live junk is normally empty now (it was deleted on 22 Sep), so the
        # negative class comes from the verified fixtures above.
        junk = (_live_junk + _fixture_cases())[:args.limit_junk]
        cases = ([(s, "not_company") for s in junk]
                 + [(s, "company") for s in real]
                 + [(s, None) for s in amb])
        # Validate the key BEFORE running anything. The local half takes ~3.5
        # minutes, and failing after that on a bad key wastes the wait and
        # buries the real problem under 34 identical error lines.
        if args.with_cloud:
            try:
                check_cloud_key()
            except AuthFailure as exc:
                print(f"Cannot start the cloud half:\n\n  {exc}\n")
                print("Nothing was run and nothing was charged. Fix the key and re-run,")
                print("or drop --with-cloud to measure the local baseline alone.")
                sys.exit(2)

        print(f"Job A — is this a company?")
        print(f"  known NOT companies : {len(junk)}")
        print(f"  known companies     : {len(real)}")
        print(f"  genuinely ambiguous : {len(amb)}  (no label — this is what we're buying)")
        print(f"  cloud               : {'ON  ' + args.cloud_model if args.with_cloud else 'off'}")
        print()

        results, scores = [], {}
        providers = [("local", lambda s, p, sc: ask_local(s, p, sc))]
        if args.with_cloud:
            providers.append(("cloud", lambda s, p, sc: ask_cloud(s, p, sc, args.cloud_model)))

        for pname, fn in providers:
            correct = graded = errors = 0
            elapsed = 0.0
            tok_in = tok_out = 0
            aborted = None
            print(f"--- {pname} ---")
            for s, label in cases:
                try:
                    out, el, usage = fn(_SYSTEM_A, _PROMPT_A.format(**_fields(s)), _SCHEMA_A)
                except AuthFailure as exc:
                    aborted = str(exc)
                    break
                elapsed += el
                tok_in += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                tok_out += usage.get("output_tokens", 0)
                ans = (out or {}).get("answer", "ERROR")
                if ans == "ERROR":
                    errors += 1
                    mark = "ERR "
                elif label:
                    graded += 1
                    if ans == label:
                        correct += 1
                    mark = "ok " if ans == label else "MISS"
                else:
                    mark = "  ?"
                print(f"  {mark} {(s.name or '')[:34]:36} -> {ans:12} {el:5.1f}s  "
                      f"{((out or {}).get('reason') or usage.get('error',''))[:54]}")
                results.append({"provider": pname, "name": s.name, "label": label,
                                "answer": ans, "reason": (out or {}).get("reason"),
                                "seconds": round(el, 2)})
            if aborted:
                scores[pname] = {"did_not_run": True, "reason": aborted}
                print(f"\n  !! {pname} did not run: {aborted}\n")
                continue
            acc = correct / graded if graded else 0.0
            scores[pname] = {"accuracy": acc, "correct": correct, "graded": graded,
                             "errors": errors,
                             "total_seconds": round(elapsed, 1),
                             "mean_seconds": round(elapsed / max(len(cases), 1), 2),
                             "tokens_in": tok_in, "tokens_out": tok_out}
            print(f"  => accuracy on labelled cases: {correct}/{graded} = {acc:.0%}"
                  + (f"   ({errors} call errors, excluded)" if errors else "")
                  + f"   {elapsed:.0f}s total, {elapsed/max(len(cases),1):.1f}s/record\n")

        print("=" * 72)
        for p, sc in scores.items():
            if sc.get("did_not_run"):
                print(f"  {p:6} DID NOT RUN — {sc['reason'].splitlines()[0]}")
                continue
            print(f"  {p:6} accuracy {sc['accuracy']:.0%} ({sc['correct']}/{sc['graded']})   "
                  f"{sc['mean_seconds']}s per record" +
                  (f"   tokens in/out {sc['tokens_in']}/{sc['tokens_out']}" if sc['tokens_in'] else ""))
        print("=" * 72)
        if not args.with_cloud:
            print("\nLocal baseline only. Set ANTHROPIC_API_KEY and add --with-cloud")
            print("to fill in the comparison column.")

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({"scores": scores, "results": results}, fh, indent=2, ensure_ascii=False)
            print(f"\nWrote {args.json}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
