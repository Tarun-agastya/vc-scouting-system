"""
Manual trigger for the nightly review resolver (Phase 2) and its research
loop (Phase 3) — plans/REVIEW_INBOX_AUTONOMY_PLAN.md. Report-only by default.

Normally the resolver runs automatically at 01:00 as a scheduler job inside
the API process (api/main.py), gated on `settings.resolver_enabled` (default
OFF — nothing runs unattended until it has been proven by hand). The
research loop (--research) is not scheduled at all yet — run it manually.
Use this CLI to run either manually first: read the output, check the
verdicts against a few real records, and only pass --apply once you'd trust
the same call in the Review Inbox UI.

--apply never overwrites a stored value or merges a record, at any
confidence — see processing/review_resolver.py's and
processing/review_researcher.py's docstrings for exactly what it does and
does not do. The only thing --apply does is close the reviews where the
model says the value we already have is right (or that two records are
different companies). Everything else is left pending, with the model's
reasoning already attached so a person can act on it in one click.

Usage
-----
    python3 scripts/resolve_reviews.py --limit 50
    python3 scripts/resolve_reviews.py --limit 50 --type field_update
    python3 scripts/resolve_reviews.py --limit 50 --apply
    python3 scripts/resolve_reviews.py --research --limit 20
        # city/website/funding_stage/address/country/contact_info only —
        # searches the web per group, capped by settings.resolver_max_searches
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=120,
                    help="max field_update groups AND max duplicate reviews to judge "
                        "(--research: max groups researched)")
    ap.add_argument("--apply", action="store_true",
                    help="close the confident reversible-direction verdicts")
    ap.add_argument("--type", choices=["field_update", "possible_duplicate"], default=None,
                    help="judge only this review type (default: both; ignored with --research)")
    ap.add_argument("--research", action="store_true",
                    help="Phase 3: search the web for the fields stored data can't settle "
                        "(city, website, funding_stage, address, country, contact_info)")
    args = ap.parse_args()

    if args.apply:
        print("--apply will close reviews where the model says the stored value")
        print("is right, or that two records are different companies. Nothing is")
        print("ever overwritten or merged. Ctrl-C now to stop.\n")

    from datetime import datetime
    from processing.review_actions import record_resolver_run

    def show(ev):
        """
        Stream each verdict as it lands. Without this the script sits silent
        for the whole run — a local 14B judgement is ~6-10s, so --limit 50
        is 10+ minutes of no output, which reads as a hang rather than as
        work in progress. Mirrors what scripts/adjudicate_field_changes.py
        and adjudicate_duplicates.py already print per item.
        """
        if ev.get("event") == "phase":
            label = ("field changes" if ev["phase"] == "field_update"
                     else "possible duplicates")
            print(f"\n── judging {ev['total']} {label} "
                  f"(~6-10s each) {'─' * 20}\n", flush=True)
            return

        name = str(ev.get("name") or "?")[:24]
        if ev.get("unavailable"):
            print(f"  {'':8} {name:26} (model unavailable)", flush=True)
            return

        mark = "->CLOSE" if ev.get("will_close") else "       "
        pos = f"[{ev.get('n')}/{ev.get('total')}]"
        if ev["phase"] == "field_update":
            print(f"  {mark} {pos:9} {name:26} {ev.get('field', ''):13} "
                  f"{ev.get('label', ''):16} {ev.get('confidence', '')}", flush=True)
            print(f"           stored: {str(ev.get('current'))[:60]}", flush=True)
            for c in (ev.get("considered") or []):
                flag = "  <- picked" if c == ev.get("winner") else ""
                print(f"           cand  : {str(c)[:60]}{flag}", flush=True)
        else:
            print(f"  {mark} {pos:9} {name:26} ~ {str(ev.get('incoming') or '?')[:22]:24} "
                  f"{ev.get('label', ''):22} {ev.get('confidence', '')}", flush=True)
            if ev.get("key_signal"):
                print(f"           via: {str(ev['key_signal'])[:60]}", flush=True)
        if ev.get("reasoning"):
            print(f"           {str(ev['reasoning'])[:92]}", flush=True)

    started = datetime.utcnow()
    if args.research:
        from processing.review_researcher import research_pending
        stats = asyncio.run(research_pending(limit=args.limit, apply=args.apply))
        record_resolver_run("research", stats, started)
    else:
        from processing.review_resolver import resolve_pending
        stats = asyncio.run(resolve_pending(limit=args.limit, apply=args.apply,
                                            only_type=args.type, on_progress=show))
        record_resolver_run("resolve", stats, started)

    print("\n" + "=" * 60)
    for k, v in sorted(stats.items()):
        print(f"  {k:24} {v}")
    print("=" * 60)
    if args.apply:
        print(f"\nClosed {stats.get('auto_closed', 0)} review(s).")
    else:
        print("\nReport only — reasoning written onto every review judged, nothing closed.")
        print("Re-run with --apply once you've checked the verdicts above.")
    if stats.get("unavailable"):
        print(f"\n{stats['unavailable']} review(s) skipped — Ollama unreachable or "
              "gave an unusable response. Re-run later to pick them up.")
    if stats.get("budget_exhausted"):
        print(f"\n{stats['budget_exhausted']} group(s) left unresearched — "
              "search budget used up for this run.")
    if "error" in stats:
        print(f"\nRun stopped early: {stats['error']}")


if __name__ == "__main__":
    main()
