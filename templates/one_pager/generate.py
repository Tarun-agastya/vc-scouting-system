"""
Generate a GT Hub one-pager draft from a pitch deck (PDF or PPTX).

    python3 templates/one_pager/generate.py --deck ~/Desktop/ONOX.pdf --name ONOX
    python3 templates/one_pager/generate.py --deck deck.pptx --name LIGARO --url https://ligaro.org
    python3 templates/one_pager/generate.py --deck deck.pdf --name X --draft-lang en
    python3 templates/one_pager/generate.py --deck deck.pdf --name X --no-llm

ALWAYS writes both languages: data/<slug>.en.yaml and data/<slug>.de.yaml, plus
data/assets/<slug>/ full of candidate images, then stops. German is the final,
exported version, so by default it is the one drafted from the deck; English is
translated from it, so the two pages state the same facts. It never exports, never publishes, and never marks anything approved —
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
import i18n  # noqa: E402
import llm as llm_mod  # noqa: E402
import websearch  # noqa: E402

logger = logging.getLogger("one_pager.generate")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
ASSETS_DIR = DATA_DIR / "assets"

# Deck text budget for one prompt. With num_ctx 16384 (llm.NUM_CTX), ~14,000
# characters fits a typical 20-30 slide deck WHOLE once footers are stripped —
# Eidola's 20 slides are 12,400. The old 6,000 sent 4 of its 20 slides and the
# drafts came out as one-line fragments. Over budget, every slide is trimmed
# evenly rather than whole slides dropped (deck.Deck.content_text).
DECK_TEXT_BUDGET = 14000
WEBSITE_TEXT_BUDGET = 4000

# Below this a section reads as a fragment, not an explanation (the finished
# GT Hub pages run 20-55 words per section).
MIN_SECTION_WORDS = {"loesung": 25, "mehrwerte": 20, "usp": 25, "zielgruppe": 20, "geschaeftsmodell": 12}

# Open questions are internal notes for whoever finishes the draft, so they are
# always English and name sections by their English heading, in both files.
SECTION_LABELS = dict(i18n.labels("en")["sections"])


def yaml_path(slug: str, lang: str, out_dir: Path = DATA_DIR) -> Path:
    return out_dir / f"{slug}.{lang}.yaml"


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
    "n/a" / "k. A." rather than a guess. This is why Hula Earth's team_size is k. A.
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


def _number_origin(bad: list, website_text: str, web) -> str:
    """Say where each figure that is not in the deck came from."""
    parts = []
    for n in bad:
        d = _digits(n)
        if website_text and d in {_digits(m.group()) for m in _NUM.finditer(website_text)}:
            parts.append(f"{n} (from the company website)")
        elif web and web.text and d in {_digits(m.group()) for m in _NUM.finditer(web.text)}:
            parts.append(f"{n} (from a web search result — third-party)")
        else:
            parts.append(f"{n} (in no source — possibly calculated by the model)")
    return ", ".join(parts)


def build_yaml(*, name, slug, drafted, deck_obj, images, url, url_ok, llm_note, lang="en",
               website_text="", web=None):
    L = i18n.labels(lang)
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
            f"Team size: the model suggested {d['team_size']!r}, but that number is not "
            f"in the deck — set to '{L['unknown']}'. Please check and fill in."
        )
    for label, val, key in (("Location", location, "location"), ("Year founded", founded, "founded")):
        if not val:
            open_questions.append(f"{label} not found in the deck — please fill in '{key}'.")

    sections = {}
    for key, heading in SECTION_LABELS.items():
        val = d.get(key)
        sections[key] = val or ""
        if not val:
            open_questions.append(f"Section '{heading}' could not be drafted from the deck — please write it.")
            continue
        words = len(val.split())
        if words < MIN_SECTION_WORDS[key]:
            open_questions.append(
                f"Section '{heading}' is only {words} words — too short for a reader who "
                f"doesn't know the company. Expand it from the deck.")
        if key == "mehrwerte" and not any(c.isdigit() for c in val):
            open_questions.append(
                f"Section '{heading}' has no concrete figure — the deck gave none the model "
                f"could use. Add one (e.g. tonnes, %, cost saving); the PowerPoint export "
                f"requires it.")
        bad = _unsupported_numbers(val, source_text)
        if bad:
            open_questions.append(
                f"Section '{heading}': figure(s) not in the deck: "
                f"{_number_origin(bad, website_text, web)}. Check before use."
            )

    if not d.get("claim"):
        open_questions.append("Claim could not be drafted — please add one as a noun phrase (max. 70 characters).")

    # Visuals: never auto-picked. Name the real candidate files so the choice
    # is a two-line edit rather than a hunt through a folder.
    if images:
        hint = ", ".join(images[:6]) + (f" … (+{len(images) - 6} more)" if len(images) > 6 else "")
        vis_note = f"Candidates in assets/{slug}/: {hint}"
        open_questions.append(
            f"Images: {len(images)} candidates were extracted to data/assets/{slug}/. "
            f"Pick two and enter them under 'visuals' as image: assets/{slug}/<file> "
            f"(in both language files)."
        )
    else:
        vis_note = f"No images could be extracted — please add them to assets/{slug}/ by hand"
        open_questions.append("No images could be extracted from the deck — please add them by hand.")

    sources = [f"Pitch deck: {deck_obj.path.name} ({len(deck_obj.slides)} slides, {deck_obj.kind.upper()})"]
    content_slides = [s.number for s in deck_obj.slides if s.is_content]
    if content_slides:
        sources.append(f"Content slides used: {', '.join(str(n) for n in content_slides)}")
    if url:
        sources.append(f"Website: {url}" + ("" if url_ok else "  [unreachable — not used]"))
    if web is not None:
        for u in web.urls:
            sources.append(f"Web search: {u}")
        spent = (f"{web.paid} Tavily credit{'s' if web.paid != 1 else ''}" if web.paid else "no paid credits")
        if web.results:
            open_questions.append(
                f"Web search added {len(web.results)} third-party source(s) ({spent}). Facts "
                f"from them are not in the deck — check them against the links under 'sources'.")
        elif web.queries:
            open_questions.append(f"Web search found nothing clearly about {name} ({spent}).")
        open_questions.extend(web.notes)

    return {
        "lang": lang,
        "meta": {"page_label": L["page_label"]},
        "claim": d.get("claim") or "",
        "name": name,
        "location": location or L["unknown"],
        "founded": founded or L["unknown"],
        "team_size": team or L["unknown"],
        "sections": sections,
        "visuals": {
            key: {"label": label, "placeholder": vis_note} for key, label in i18n.visuals(lang)
        },
        "sources": sources,
        "review": {"status": "draft", "open_questions": open_questions},
    }


def translate_data(src: dict, dst: str, *, src_label: str = "") -> tuple:
    """
    The `dst`-language twin of a one-pager dict: same facts, images and sources;
    claim, meta line and sections translated by the local model.

    Returns (data, ok). ok=False means the model call failed: the twin is still
    complete and renderable-in-shape, with empty prose and an open question
    saying how to retry — the same "never cost the run" rule as drafting.
    """
    import copy

    src_lang = i18n.lang_of(src)
    S, D = i18n.labels(src_lang), i18n.labels(dst)
    out = copy.deepcopy(src)
    out["lang"] = dst

    fields = {k: src.get(k) for k in ("claim", "location", "founded", "team_size")}
    fields.update((k, (src.get("sections") or {}).get(k)) for k in i18n.SECTION_KEYS)
    for k in ("location", "founded", "team_size"):
        if i18n.is_unknown(fields[k]):
            fields[k] = ""

    tr = llm_mod.translate(fields, src_lang, dst)
    ok = tr is not None
    tr = tr or {}
    for k in ("location", "founded", "team_size"):
        if not fields[k]:
            out[k] = D["unknown"]
        else:   # untranslated but true beats empty
            out[k] = str(tr.get(k) or fields[k])
    # team_size and founded are numbers; never let a translation change them.
    for k in ("founded", "team_size"):
        if fields[k] and _digits(str(fields[k])) and _digits(str(out[k])) != _digits(str(fields[k])):
            out[k] = str(fields[k])
    out["claim"] = tr.get("claim") or ""
    out["sections"] = {k: tr.get(k) or "" for k in i18n.SECTION_KEYS}

    meta = dict(out.get("meta") or {})
    if meta.get("page_label") in (None, "", S["page_label"]):
        meta["page_label"] = D["page_label"]
    out["meta"] = meta
    vis = {}
    for key in i18n.VISUAL_KEYS:
        slot = dict((src.get("visuals") or {}).get(key) or {})
        if slot.get("label") in (None, "", S["visuals"][key]):
            slot["label"] = D["visuals"][key]
        vis[key] = slot
    out["visuals"] = vis

    src_name = i18n.labels(src_lang)["name"]
    carried = list((src.get("review") or {}).get("open_questions") or [])
    questions = []
    if ok:
        questions.append(
            f"Translated automatically from the {src_name} version{src_label} — "
            f"read it through before use.")
        source_text = " ".join(str(v or "") for v in fields.values())
        for key, heading in SECTION_LABELS.items():
            bad = _unsupported_numbers(out["sections"][key], source_text)
            if bad:
                questions.append(
                    f"Section '{heading}': the translation contains {', '.join(bad)}, "
                    f"which the {src_name} version does not. Check it.")
        bad = _unsupported_numbers(out["claim"], source_text)
        if bad:
            questions.append(f"Claim: the translation contains {', '.join(bad)}, which the {src_name} version does not.")
    elif any(str(v or "").strip() for v in fields.values()):
        questions.append(
            f"Translation from the {src_name} version failed (local model unavailable). "
            f"Retry from the One-Pager page, or run: python3 templates/one_pager/translate.py "
            f"<the {src_lang} yaml> --force")
    out["review"] = {"status": "draft", "open_questions": questions + carried}
    return out, ok


def write_yaml(data: dict, path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        header + yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88),
        encoding="utf-8",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--deck", required=True, help="pitch deck: .pdf or .pptx")
    ap.add_argument("--name", required=True, help="startup name as it should appear")
    ap.add_argument("--url", default=None, help="optional company website for extra context")
    ap.add_argument("--draft-lang", choices=i18n.LANGS, default=i18n.FINAL_LANG,
                    help="language drafted from the deck (default: de, the exported one); the other one is translated from it")
    ap.add_argument("--out-dir", default=None, help="where to write the YAMLs (default: data/)")
    ap.add_argument("--no-llm", action="store_true", help="skip drafting; extract deck + images only")
    ap.add_argument("--no-web-search", action="store_true", help="don't search the web at all")
    ap.add_argument("--no-paid-search", action="store_true",
                    help="search only free/cached sources; never spend Tavily credits")
    ap.add_argument("--force", action="store_true", help="overwrite existing drafts")
    args = ap.parse_args()

    slug = slugify(args.name)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else DATA_DIR
    primary, secondary = args.draft_lang, i18n.other(args.draft_lang)
    paths = {lang: yaml_path(slug, lang, out_dir) for lang in i18n.LANGS}
    legacy = out_dir / f"{slug}.yaml"          # one-language file from before both existed
    existing = [p for p in (*paths.values(), legacy) if p.exists()]
    if existing and not args.force:
        print(f"✗ {existing[0]} already exists. Re-run with --force to overwrite.")
        return 1

    # 1. Deck — the only hard failure.
    try:
        d = deck_mod.parse_deck(args.deck)
    except deck_mod.DeckError as exc:
        print(f"✗ {exc}")
        return 1
    content = [s for s in d.slides if s.is_content]
    print(f"✓ Deck read: {d.path.name} — {len(d.slides)} slides, {len(content)} with content")

    # 2. Images — best effort.
    images = deck_mod.extract_images(d, ASSETS_DIR / slug)
    print(f"✓ Images extracted: {len(images)} to data/assets/{slug}/")

    # 3. Website — optional, non-fatal.
    extra, url_ok = "", False
    if args.url:
        try:
            import trafilatura

            dl = trafilatura.fetch_url(args.url)
            extra = (trafilatura.extract(dl) or "")[:WEBSITE_TEXT_BUDGET] if dl else ""
            url_ok = bool(extra.strip())
            print(f"{'✓' if url_ok else '!'} Website {args.url}: {len(extra)} characters")
        except Exception as exc:
            logger.warning(f"Website {args.url} unreachable ({exc}) — continuing without it")

    # 3b. Web search — optional, credit-guarded (see websearch.py), non-fatal.
    web = None
    if not args.no_web_search and not args.no_llm:
        web = websearch.search_company(args.name, args.url, allow_paid=not args.no_paid_search)
        how = f"{web.cached} cached, {web.free} free, {web.paid} paid (Tavily)"
        print(f"✓ Web search: {len(web.results)} relevant results — {how}")
        for n in web.notes:
            print(f"! {n}")

    # 4. Draft — degrades to empty sections.
    drafted, llm_note, llm_ok = None, None, False
    if args.no_llm:
        llm_note = "Created with --no-llm: every section is deliberately empty and must be written by hand."
        print("• Model skipped (--no-llm)")
    else:
        unhealthy = llm_mod.health()
        if unhealthy:
            llm_note = f"Sections could not be drafted: {unhealthy}. Please write them by hand."
            print(f"! {unhealthy} — continuing without a text draft")
        else:
            print(f"• Drafting ({i18n.labels(primary)['name']}) with {llm_mod.MODEL} …")
            drafted = llm_mod.draft(args.name, d.content_text(DECK_TEXT_BUDGET), extra, lang=primary,
                                    web_text=web.text if web else "")
            if drafted is None:
                llm_note = "Sections could not be drafted (the local model returned nothing usable). Please write them by hand."
                print("! Draft failed — the YAML files are still written")
            else:
                llm_ok = True
                print("✓ Draft written")

    # 5. Ground and assemble the drafted language.
    data = {primary: build_yaml(name=args.name, slug=slug, drafted=drafted, deck_obj=d,
                                images=images, url=args.url, url_ok=url_ok,
                                llm_note=llm_note, lang=primary,
                                website_text=extra, web=web)}

    # 6. The other language: translated from it, never drafted separately.
    if llm_ok:
        print(f"• Translating to {i18n.labels(secondary)['name']} …")
    data[secondary], tr_ok = translate_data(data[primary], secondary) if llm_ok \
        else (_empty_twin(data[primary], secondary), False)
    if llm_ok:
        print("✓ Translation written" if tr_ok else "! Translation failed — the file is still written, with empty sections")

    # 7. Write both.
    for lang in i18n.LANGS:
        header = (
            f"# GT Hub One-Pager ({i18n.labels(lang)['name']}) — DRAFT, generated from {d.path.name}.\n"
            f"# Written by templates/one_pager/generate.py. Nothing here is checked yet:\n"
            f"# every item under review.open_questions needs a person.\n"
            f"# Its twin in the other language: {paths[i18n.other(lang)].name}\n"
            f"# Format and rules: templates/one_pager/FORMAT.md\n\n"
        )
        write_yaml(data[lang], paths[lang], header)
    if legacy.exists():
        # --force over a pre-two-language draft: keep it, out of the way.
        legacy.rename(legacy.with_suffix(".yaml.replaced"))

    for lang in i18n.LANGS:
        print(f"✓ Written ({lang}): {paths[lang]}")
    q = data[primary]["review"]["open_questions"]
    print(f"\n  {len(q)} open items ({primary}):")
    for item in q:
        print(f"   • {' '.join(str(item).split())[:150]}")
    print("\nNext steps:")
    print(f"  1. Pick two images from data/assets/{slug}/ and enter them under 'visuals' in both files")
    print("  2. Work through the open items above")
    print(f"  3. python3 templates/one_pager/render.py {paths[i18n.FINAL_LANG]} --check")
    print("  4. python3 templates/one_pager/export_pptx.py <yaml>   # editable PowerPoint")
    return 0


def _empty_twin(src: dict, dst: str) -> dict:
    """The other-language file when there was no prose to translate."""
    import copy

    D = i18n.labels(dst)
    out = copy.deepcopy(src)
    out["lang"] = dst
    out["meta"] = {**(src.get("meta") or {}), "page_label": D["page_label"]}
    for k in ("location", "founded", "team_size"):
        if i18n.is_unknown(out.get(k)):
            out[k] = D["unknown"]
    out["visuals"] = {k: {**((src.get("visuals") or {}).get(k) or {}), "label": label}
                      for k, label in i18n.visuals(dst)}
    out["review"] = {"status": "draft",
                     "open_questions": list((src.get("review") or {}).get("open_questions") or [])}
    return out


if __name__ == "__main__":
    sys.exit(main())
