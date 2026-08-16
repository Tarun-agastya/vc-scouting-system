"""
Generate a GT Hub one-pager draft from a pitch deck (PDF or PPTX).

    python3 templates/one_pager/generate.py --deck ~/Desktop/ONOX.pdf --name ONOX
    python3 templates/one_pager/generate.py --deck deck.pptx --name LIGARO --url https://ligaro.org
    python3 templates/one_pager/generate.py --deck deck.pdf --name X --no-llm

Writes data/<slug>.yaml plus data/assets/<slug>/ full of candidate images, then
stops. It never exports, never publishes, and never marks anything approved —
the draft is a starting point a human finishes. That is the same staged model
the Review Inbox uses, for the same reason: a one-pager is outward-facing.

ISOLATION CONTRACT (see FORMAT.md §7): imports nothing from the VC-scouting
pipeline — no processing/, ingestion/, api/, database/, vector_db/, reasoning/
or config/. Opens no database. Writes only inside templates/one_pager/. If the
scouting pipeline is broken, mid-refactor, or entirely stopped, this still
runs. tests/test_one_pager_isolation.py enforces that automatically.

FAILURE MODEL — only an unreadable deck is fatal, because then there is no job
to do. Everything else degrades and still produces a usable draft:
  Ollama down/busy   -> sections left empty, reason recorded in open_questions
  --url unreachable  -> warn, continue from the deck alone
  no images in deck  -> placeholders kept, noted
  unsupported number -> flagged in open_questions, never silently deleted
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import deck as deck_mod  # noqa: E402
import llm as llm_mod  # noqa: E402

logger = logging.getLogger("one_pager.generate")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
ASSETS_DIR = DATA_DIR / "assets"

# Deck text budget for one prompt. num_ctx is 8192 tokens; ~6000 chars of
# German leaves comfortable room for the system prompt, the rules block and a
# 1200-token answer. Content-dense slides are chosen first (see
# deck.Deck.content_text) so the budget is never spent on chapter dividers.
DECK_TEXT_BUDGET = 6000

SECTION_LABELS = {
    "loesung": "Lösung & Funktionalität",
    "mehrwerte": "Mehrwerte & Leistungen",
    "usp": "USP & Abgrenzung vom Wettbewerb",
    "zielgruppe": "Zielgruppe & Kunden",
    "geschaeftsmodell": "Geschäftsmodell",
}


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "startup"


# ── Grounding ────────────────────────────────────────────────────────────────
# Mirrors reasoning/qwen_client.py::_ground_startup's discipline without
# importing it: gate only what is both fabrication-prone AND checkable, never
# gate paraphrase. The five sections ARE paraphrase — rewriting the deck in
# GT Hub's voice is the entire job — so they are never auto-nulled. Numbers
# inside them are checkable, so they are checked.

_NUM = re.compile(r"\d[\d.,]*\d|\d")


def _digits(token: str) -> str:
    return re.sub(r"\D", "", token)


def _unsupported_numbers(text: str, source: str) -> list:
    """
    Numbers in `text` with no counterpart in `source`.

    Compared digits-only so German/English thousand separators can't cause a
    false alarm ("95.861" vs "95,861"). Caught a real case on the first live
    run: from a deck stating 121.300 EUR and 95.861 EUR, the model wrote
    "25.439 EUR gesenkt" — arithmetic it performed itself, correct but not
    stated anywhere in the source. Exactly the class of number a reader would
    assume came from the deck.
    """
    if not text:
        return []
    src = {_digits(m.group()) for m in _NUM.finditer(source)}
    src.discard("")
    bad = []
    for m in _NUM.finditer(text):
        d = _digits(m.group())
        if d and d not in src and m.group() not in bad:
            bad.append(m.group())
    return bad


def _supported_meta(value, source: str):
    """
    Meta-line fields are the same class as founded_year/employee_count in the
    pipeline's own grounding gate: short, factual, checkable. Unsupported ->
    "k. A." rather than a guess. This is why Hula Earth's team_size is k. A.
    today, and it must stay that way rather than becoming a plausible number.
    """
    if not value:
        return None
    return value if _digits(str(value)) in {_digits(m.group()) for m in _NUM.finditer(source)} \
        or not _digits(str(value)) else None


# ── YAML emission ────────────────────────────────────────────────────────────

def _folded(dumper, data):
    """Long prose as folded block scalars, so the file stays hand-editable."""
    style = ">" if len(data) > 80 and "\n" not in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, _folded)


def build_yaml(*, name, slug, drafted, deck_obj, images, url, url_ok, llm_note):
    source_text = deck_obj.full_text
    open_questions = []
    d = drafted or {}

    if llm_note:
        open_questions.append(llm_note)

    # Meta fields: keep only what the deck actually supports.
    location = d.get("location") or None
    founded = d.get("founded") or None
    team = _supported_meta(d.get("team_size"), source_text)
    if d.get("team_size") and not team:
        open_questions.append(
            f"Teamgröße: Das Modell schlug {d['team_size']!r} vor, diese Zahl steht "
            f"so aber nicht im Deck — auf 'k. A.' gesetzt. Bitte prüfen und eintragen."
        )
    for label, val, key in (("Ort", location, "location"), ("Gründungsjahr", founded, "founded")):
        if not val:
            open_questions.append(f"{label} nicht im Deck gefunden — bitte in '{key}' eintragen.")

    sections = {}
    for key, heading in SECTION_LABELS.items():
        val = d.get(key)
        sections[key] = val or ""
        if not val:
            open_questions.append(f"Abschnitt '{heading}' konnte nicht aus dem Deck erzeugt werden — bitte schreiben.")
            continue
        bad = _unsupported_numbers(val, source_text)
        if bad:
            open_questions.append(
                f"Abschnitt '{heading}': die Zahl(en) {', '.join(bad)} stehen nicht im Deck "
                f"(ggf. vom Modell errechnet). Bitte gegen das Deck prüfen oder streichen."
            )

    if not d.get("claim"):
        open_questions.append("Claim konnte nicht erzeugt werden — bitte als Nominalphrase ergänzen (max. 70 Zeichen).")

    # Visuals: never auto-picked. Name the real candidate files so the choice
    # is a two-line edit rather than a hunt through a folder.
    if images:
        hint = ", ".join(images[:6]) + (f" … (+{len(images) - 6} weitere)" if len(images) > 6 else "")
        vis_note = f"Kandidaten in assets/{slug}/: {hint}"
        open_questions.append(
            f"Bilder: {len(images)} Kandidaten wurden nach data/assets/{slug}/ extrahiert. "
            f"Bitte zwei auswählen und bei 'visuals' als image: assets/{slug}/<datei> eintragen."
        )
    else:
        vis_note = f"Keine Bilder extrahierbar — bitte manuell in assets/{slug}/ ablegen"
        open_questions.append(f"Aus dem Deck liessen sich keine Bilder extrahieren — bitte manuell ergänzen.")

    sources = [f"Pitch Deck: {deck_obj.path.name} ({len(deck_obj.slides)} Folien, {deck_obj.kind.upper()})"]
    content_slides = [s.number for s in deck_obj.slides if s.is_content]
    if content_slides:
        sources.append(f"Inhaltsfolien ausgewertet: {', '.join(str(n) for n in content_slides)}")
    if url:
        sources.append(f"Website: {url}" + ("" if url_ok else "  [nicht erreichbar — nicht ausgewertet]"))

    return {
        "meta": {"page_label": "Matchmaking-Startups"},
        "claim": d.get("claim") or "",
        "name": name,
        "location": location or "k. A.",
        "founded": founded or "k. A.",
        "team_size": team or "k. A.",
        "sections": sections,
        "visuals": {
            "visual_solution": {"label": "Visualisierung der Lösung", "placeholder": vis_note},
            "visual_how_it_works": {"label": "So funktioniert die Lösung", "placeholder": vis_note},
        },
        "sources": sources,
        "review": {"status": "draft", "open_questions": open_questions},
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--deck", required=True, help="pitch deck: .pdf or .pptx")
    ap.add_argument("--name", required=True, help="startup name as it should appear")
    ap.add_argument("--url", default=None, help="optional company website for extra context")
    ap.add_argument("--out", default=None, help="output YAML (default: data/<slug>.yaml)")
    ap.add_argument("--no-llm", action="store_true", help="skip drafting; extract deck + images only")
    ap.add_argument("--force", action="store_true", help="overwrite an existing YAML")
    args = ap.parse_args()

    slug = slugify(args.name)
    out_path = Path(args.out).resolve() if args.out else DATA_DIR / f"{slug}.yaml"
    if out_path.exists() and not args.force:
        print(f"✗ {out_path} already exists. Re-run with --force to overwrite, or pass --out.")
        return 1

    # 1. Deck — the only hard failure.
    try:
        d = deck_mod.parse_deck(args.deck)
    except deck_mod.DeckError as exc:
        print(f"✗ {exc}")
        return 1
    content = [s for s in d.slides if s.is_content]
    print(f"✓ Deck gelesen: {d.path.name} — {len(d.slides)} Folien, {len(content)} mit Inhalt")

    # 2. Images — best effort.
    images = deck_mod.extract_images(d, ASSETS_DIR / slug)
    print(f"✓ Bilder extrahiert: {len(images)} nach data/assets/{slug}/")

    # 3. Website — optional, non-fatal.
    extra, url_ok = "", False
    if args.url:
        try:
            import trafilatura

            dl = trafilatura.fetch_url(args.url)
            extra = (trafilatura.extract(dl) or "")[:3000] if dl else ""
            url_ok = bool(extra.strip())
            print(f"{'✓' if url_ok else '!'} Website {args.url}: {len(extra)} Zeichen")
        except Exception as exc:
            logger.warning(f"Website {args.url} nicht abrufbar ({exc}) — weiter ohne")

    # 4. Draft — degrades to empty sections.
    drafted, llm_note = None, None
    if args.no_llm:
        llm_note = "Mit --no-llm erzeugt: alle Abschnitte sind bewusst leer und von Hand zu schreiben."
        print("• LLM übersprungen (--no-llm)")
    else:
        unhealthy = llm_mod.health()
        if unhealthy:
            llm_note = f"Abschnitte konnten nicht erzeugt werden: {unhealthy}. Bitte von Hand schreiben."
            print(f"! {unhealthy} — weiter ohne Textentwurf")
        else:
            print(f"• Entwurf über {llm_mod.MODEL} …")
            drafted = llm_mod.draft(args.name, d.content_text(DECK_TEXT_BUDGET), extra)
            if drafted is None:
                llm_note = "Abschnitte konnten nicht erzeugt werden (Ollama antwortete nicht verwertbar). Bitte von Hand schreiben."
                print("! Entwurf fehlgeschlagen — YAML wird trotzdem geschrieben")
            else:
                print("✓ Entwurf erzeugt")

    # 5+6. Ground, assemble, write.
    data = build_yaml(name=args.name, slug=slug, drafted=drafted, deck_obj=d,
                      images=images, url=args.url, url_ok=url_ok, llm_note=llm_note)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# GT Hub One-Pager — ENTWURF, automatisch erzeugt aus {d.path.name}.\n"
        f"# Erzeugt von templates/one_pager/generate.py. Nichts hier ist geprüft:\n"
        f"# jeder Punkt unter review.open_questions braucht einen Menschen.\n"
        f"# Format und Regeln: templates/one_pager/FORMAT.md\n\n"
    )
    out_path.write_text(
        header + yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88),
        encoding="utf-8",
    )

    q = data["review"]["open_questions"]
    print(f"\n✓ Geschrieben: {out_path}")
    print(f"  {len(q)} offene Punkte:")
    for item in q:
        print(f"   • {' '.join(str(item).split())[:150]}")
    print("\nNächste Schritte:")
    print(f"  1. Zwei Bilder aus data/assets/{slug}/ auswählen und bei 'visuals' eintragen")
    print(f"  2. Offene Punkte oben abarbeiten")
    print(f"  3. python3 templates/one_pager/render.py {out_path.relative_to(Path.cwd()) if out_path.is_relative_to(Path.cwd()) else out_path} --check")
    print(f"  4. python3 templates/one_pager/export_pptx.py <yaml>   # editierbares PowerPoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
