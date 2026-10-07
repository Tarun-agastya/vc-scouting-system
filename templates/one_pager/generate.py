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
import json
import logging
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import deck as deck_mod  # noqa: E402
import i18n  # noqa: E402
import llm as llm_mod  # noqa: E402
import logo_fetch  # noqa: E402
import research  # noqa: E402
import sitereader  # noqa: E402
import websearch  # noqa: E402

logger = logging.getLogger("one_pager.generate")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
ASSETS_DIR = DATA_DIR / "assets"
# Copies of uploaded decks, so the dashboard can regenerate a one-pager with
# new instructions without asking for the deck again. Gitignored: pitch decks
# are confidential and never belong in the repository.
DECKS_DIR = DATA_DIR / "decks"

# What the GT Hub team can set by hand (dashboard fields / CLI flags). A value
# given here beats every source — deck, website, web search — and survives
# regeneration until someone changes it. Stored under `manual:` in the YAML.
MANUAL_FIELDS = ("location", "founded", "team_size", "notes")

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
    nums = re.findall(r"\d+", str(value))
    if not nums:
        return value
    have = {_digits(m.group()) for m in _NUM.finditer(source)}
    # Every number must be in a source: a range "11–50" needs both 11 and 50.
    return value if all(n in have for n in nums) else None


_TEAM_WORD = re.compile(r"(employees?|mitarbeit\w*|people|personen|köpfe|team\s*members?|"
                        r"teammitglied\w*|fte|beschäftigte\w*|team)", re.I)


def _supported_team(value, source: str):
    """
    Like _supported_meta, but a team number must appear NEAR a word like
    "employees" / "Mitarbeiter" / "team" in a source — not just anywhere.
    Found live (Bliro, Oct 2026): "6" was in the material three times, twice
    as "6 bis 8 Stunden"; only one mention ("bliro has 6 total employees")
    actually supported it. A bare-digit check passes either way.
    """
    if not value:
        return None
    nums = re.findall(r"\d+", str(value))
    if not nums:
        return value
    near = set()
    for m in _TEAM_WORD.finditer(source):
        window = source[max(0, m.start() - 60): m.end() + 60]
        near.update(_digits(x.group()) for x in _NUM.finditer(window))
    return value if all(n in near for n in nums) else None


def _supported_founded(value, source: str):
    """A founding year must appear next to a founding word in a source ("born
    in 2023", "gegründet 2021", "a 2023 spin-off") — a year alone appears in
    every news article's date line."""
    if not value:
        return None
    years = re.findall(r"(?:19|20)\d\d", str(value))
    if not years:
        return _supported_meta(value, source)
    near = set()
    for m in re.finditer(research.FOUNDED_PATTERN, source, re.I):
        near.update(re.findall(r"(?:19|20)\d\d", m.group(0)))
    return value if all(y in near for y in years) else None


def manual_prompt_text(manual: dict) -> str:
    """The [GT Hub input] block for the model: confirmed values first, then the
    free text (facts and instructions) exactly as the team wrote it."""
    manual = manual or {}
    lines = []
    for key, label in (("location", "Location / Standort"), ("founded", "Founded / Gründung"),
                       ("team_size", "Team size / Teamgröße")):
        if manual.get(key):
            lines.append(f"{label}: {manual[key]}")
    if manual.get("notes"):
        lines.append(str(manual["notes"]).strip())
    return "\n".join(lines)


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
               website_text="", web=None, manual=None, logo=None, logo_source=None,
               logo_note=None, research_=None):
    L = i18n.labels(lang)
    manual = {k: v for k, v in (manual or {}).items() if v}
    # Your own input is a source too: a figure you typed is never flagged.
    source_text = deck_obj.full_text + "\n" + manual_prompt_text(manual)
    meta_source = "\n".join([source_text, website_text or "", web.text if web else ""])
    open_questions = []
    d = drafted or {}

    if llm_note:
        open_questions.append(llm_note)

    # Meta fields: your input first; otherwise only what a source supports.
    location = manual.get("location") or d.get("location") or None
    founded = manual.get("founded") or _supported_founded(d.get("founded"), meta_source)
    if not manual.get("founded") and d.get("founded") and not founded:
        open_questions.append(
            f"Year founded: the model suggested {d['founded']!r}, but no source states it as the founding year — "
            f"left as '{L['unknown']}'. Add it in the 'Founded' field if you know it.")
    if manual.get("team_size"):
        team = manual["team_size"]
    else:
        team = _supported_team(d.get("team_size"), meta_source)
        if d.get("team_size") and not team:
            open_questions.append(
                f"Team size: the model estimated {d['team_size']!r}, but no source states that "
                f"number — left as '{L['unknown']}'. If it's right, enter it in the 'Team size' field.")
        elif not d.get("team_size"):
            open_questions.append(
                "Team size not found in any source (deck, website, web search) — enter it in the "
                "'Team size' field (an approximate number is fine).")
    for label, val, field in (("Location", location, "Location"), ("Year founded", founded, "Founded")):
        if not val:
            open_questions.append(
                f"{label} not found in any source (deck, website, web search) — enter it in the "
                f"'{field}' field.")

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
                f"Section '{heading}': figure(s) not in the deck or on the company's own website: "
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
        open_questions.append(
            "No usable images were found on the website — please add two by hand."
            if research_ is not None else
            "No images could be extracted from the deck — please add them by hand.")

    if research_ is not None:
        sources = deck_obj.source_lines()
        if research_.website_via and research_.website_via not in ("your input", "database", "an earlier run"):
            open_questions.insert(0,
                f"Website {url} was identified automatically ({research_.website_via}; its domain "
                f"matches the name) — confirm it is the company's own site.")
        open_questions.extend(research.missing_questions(research_))
        open_questions.extend(n for n in research_.notes if n not in open_questions)
    else:
        sources = [f"Pitch deck: {deck_obj.path.name} ({len(deck_obj.slides)} slides, {deck_obj.kind.upper()})"]
        content_slides = [s.number for s in deck_obj.slides if s.is_content]
        if content_slides:
            sources.append(f"Content slides used: {', '.join(str(n) for n in content_slides)}")
    if url:
        sources.append(f"Website: {url}" + ("" if url_ok else "  [unreachable — not used]"))
    if logo:
        sources.append(f"Logo: {logo_source}")
        if not manual.get("logo"):
            open_questions.append(
                f"Logo taken automatically from {logo_source} — check it is the startup's "
                f"current logo before export (upload a better one in the 'Logo' field).")
    else:
        open_questions.append(
            f"No logo: {logo_note or 'none found.'} Upload one in the 'Logo' field, or save it "
            f"under data/assets/{slug}/ and set 'logo:'.")
    if web is not None:
        for u in web.urls:
            sources.append(f"Web search: {u}")
        spent = (f"{web.paid} Tavily credit{'s' if web.paid != 1 else ''}" if web.paid else "no paid credits")
        if web.results:
            open_questions.append(
                f"Web search added {len(web.results)} third-party source(s) ({spent}). Facts "
                f"from them come from third parties — check them against the links under 'sources'.")
        elif web.queries:
            open_questions.append(f"Web search found nothing clearly about {name} ({spent}).")
        open_questions.extend(n for n in web.notes if n not in open_questions)

    out = {
        "lang": lang,
        "meta": {"page_label": L["page_label"]},
        "claim": d.get("claim") or "",
        "name": name,
        "website": url or None,
        "location": location or L["unknown"],
        "founded": founded or L["unknown"],
        "team_size": team or L["unknown"],
        "logo": logo or None,
        "sections": sections,
        "visuals": {
            key: {"label": label, "placeholder": vis_note} for key, label in i18n.visuals(lang)
        },
        "sources": sources,
        "manual": manual or None,
        "source_record": ({"id": deck_obj.rec.get("id"), "name": deck_obj.rec.get("name")}
                          if research_ is not None else None),
        "research": research_.summary if research_ is not None else None,
        "review": {"status": "draft", "open_questions": open_questions},
    }
    return {k: v for k, v in out.items() if v is not None}


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
    # Your own values appear exactly as typed in both languages.
    manual = src.get("manual") or {}
    for k in ("location", "founded", "team_size"):
        if manual.get(k):
            out[k] = str(manual[k])
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
    ap.add_argument("--deck", default=None, help="pitch deck: .pdf or .pptx")
    ap.add_argument("--record", default=None,
                    help="instead of a deck: a HubDrive database record as JSON (written by the API) — "
                         "used as a lead; the facts come from the company's website and the web")
    ap.add_argument("--name", default=None, help="startup name as it should appear (default: the record's)")
    ap.add_argument("--url", default=None, help="optional company website for extra context")
    ap.add_argument("--draft-lang", choices=i18n.LANGS, default=i18n.FINAL_LANG,
                    help="language drafted from the deck (default: de, the exported one); the other one is translated from it")
    ap.add_argument("--out-dir", default=None, help="where to write the YAMLs (default: data/)")
    ap.add_argument("--no-llm", action="store_true", help="skip drafting; extract deck + images only")
    ap.add_argument("--no-web-search", action="store_true", help="don't search the web at all")
    ap.add_argument("--no-paid-search", action="store_true",
                    help="search only free/cached sources; never spend Tavily credits")
    ap.add_argument("--force", action="store_true", help="overwrite existing drafts")
    # Your own input. Each beats every source and is kept on regeneration.
    ap.add_argument("--location", default=None, help="the startup's location, as it should appear")
    ap.add_argument("--founded", default=None, help="year founded, as it should appear")
    ap.add_argument("--team-size", default=None, help="team size, e.g. 12 or ca. 10")
    ap.add_argument("--notes", default=None,
                    help="free text from the GT Hub team: extra facts the deck lacks, and "
                         "instructions (what to emphasise or leave out)")
    ap.add_argument("--logo", default=None, help="the startup's logo file; beats the one found online")
    ap.add_argument("--manual-replace", action="store_true",
                    help="take --location/--founded/--team-size/--notes exactly as given (empty "
                         "clears them) instead of keeping earlier values")
    ap.add_argument("--save-deck", action="store_true",
                    help="keep a copy of the deck in data/decks/ so the dashboard can regenerate")
    args = ap.parse_args()

    if bool(args.deck) == bool(args.record):
        print("✗ Give exactly one of --deck (a pitch deck) or --record (a database record).")
        return 1
    rec = None
    if args.record:
        try:
            rec = json.loads(Path(args.record).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"✗ The database record could not be read ({exc}).")
            return 1
        args.name = args.name or rec.get("name")
    if not (args.name or "").strip():
        print("✗ A startup name is required.")
        return 1
    args.name = args.name.strip()
    slug = slugify(args.name)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else DATA_DIR
    primary, secondary = args.draft_lang, i18n.other(args.draft_lang)
    paths = {lang: yaml_path(slug, lang, out_dir) for lang in i18n.LANGS}
    legacy = out_dir / f"{slug}.yaml"          # one-language file from before both existed
    existing = [p for p in (*paths.values(), legacy) if p.exists()]
    if existing and not args.force:
        print(f"✗ {existing[0]} already exists. Re-run with --force to overwrite.")
        return 1

    # Your input: what was given now, plus (unless --manual-replace) whatever
    # was given last time — so regenerating never silently loses a team size
    # someone typed in last week.
    old = _read_existing(paths, legacy) if existing else {}
    old_manual = dict(old.get("manual") or {})
    given = {k: (getattr(args, k) or "").strip() for k in MANUAL_FIELDS if getattr(args, k) is not None}
    if args.manual_replace:
        manual = {k: v for k, v in given.items() if v}
    else:
        manual = {k: v for k, v in old_manual.items() if k in MANUAL_FIELDS and v}
        manual.update({k: v for k, v in given.items() if v})
    if args.logo:
        if logo_fetch.save_local(args.logo, ASSETS_DIR / slug / "logo_manual.png"):
            manual["logo"] = f"assets/{slug}/logo_manual.png"
        else:
            print(f"! Logo file {args.logo} could not be read as an image — ignored")
    elif old_manual.get("logo") and (DATA_DIR / old_manual["logo"]).exists():
        manual["logo"] = old_manual["logo"]
    url = (args.url or "").strip() or old.get("website") or None
    if manual:
        print(f"✓ Your input: {', '.join(sorted(manual))}")

    web = None
    extra, url_ok = "", False
    res = None
    if rec is None:
        # 1. Deck — the only hard failure.
        try:
            d = deck_mod.parse_deck(args.deck)
        except deck_mod.DeckError as exc:
            print(f"✗ {exc}")
            return 1
        content = [s for s in d.slides if s.is_content]
        print(f"✓ Deck read: {d.path.name} — {len(d.slides)} slides, {len(content)} with content")
        if args.save_deck:
            _save_deck(Path(args.deck), slug)

        # 2. Images — best effort.
        images = deck_mod.extract_images(d, ASSETS_DIR / slug)
        print(f"✓ Images extracted: {len(images)} to data/assets/{slug}/")

        # 3. Website — optional, non-fatal.
        if url:
            try:
                import trafilatura

                dl = trafilatura.fetch_url(url)
                extra = (trafilatura.extract(dl) or "")[:WEBSITE_TEXT_BUDGET] if dl else ""
                url_ok = bool(extra.strip())
                print(f"{'✓' if url_ok else '!'} Website {url}: {len(extra)} characters")
            except Exception as exc:
                logger.warning(f"Website {url} unreachable ({exc}) — continuing without it")

        # 3b. Web search — optional, credit-guarded (see websearch.py), non-fatal.
        if not args.no_web_search and not args.no_llm:
            web = websearch.search_company(args.name, url, allow_paid=not args.no_paid_search)
            how = f"{web.cached} cached, {web.free} free, {web.paid} paid (Tavily)"
            print(f"✓ Web search: {len(web.results)} relevant results — {how}")
            for n in web.notes:
                print(f"! {n}")
    else:
        # 1-3. No deck: the record is a lead; research.py gathers the facts from
        # the source article, the company's own website and targeted searches
        # for whatever a one-pager needs that is still missing.
        print(f"✓ Database record: {args.name} (id {rec.get('id')})")
        res = research.run(rec, website=(args.url or "").strip() or old.get("website"),
                           website_via="your input" if args.url else "an earlier run",
                           allow_web=not args.no_web_search,
                           allow_paid=not args.no_paid_search)
        web, url = res.web, res.website
        url_ok = bool(res.site and res.site.ok)
        d = _RecordSource(rec, res)
        images = sitereader.save_site_images(res.site.image_urls, ASSETS_DIR / slug) if url_ok else []
        print(f"✓ Images from the website: {len(images)} to data/assets/{slug}/")
        if web:
            how = f"{web.cached} cached, {web.free} free, {web.paid} paid (Tavily)"
            print(f"✓ Web search: {len(web.results)} relevant results — {how}")
        missing = ", ".join(research._label(k) for k in res.missing) or "nothing"
        print(f"• Still missing after research: {missing}")

    # 3a. Logo — yours if given, else the one the website declares (logo_fetch:
    # markup-based, no model). Best effort; never blocks the draft.
    logo = logo_source = logo_note = None
    auto_logo = ASSETS_DIR / slug / "logo.png"
    if manual.get("logo"):
        logo, logo_source = manual["logo"], "provided by GT Hub"
        print("✓ Logo: your upload")
    elif url:
        r = logo_fetch.fetch_logo(url, auto_logo)
        if r["found"]:
            logo, logo_source = f"assets/{slug}/logo.png", r["source"]
            print(f"✓ Logo: {r['source']}")
        elif auto_logo.exists():
            logo, logo_source = f"assets/{slug}/logo.png", "the website (found on an earlier run)"
            print("! Logo: not found this time — keeping the one found earlier")
        else:
            logo_note = r["note"]
            print(f"! Logo: {r['note']}")
    else:
        logo_note = "no website was given, so none could be looked up."

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
            if res is None:
                drafted = llm_mod.draft(args.name, d.content_text(DECK_TEXT_BUDGET), extra, lang=primary,
                                        web_text=web.text if web else "",
                                        manual_text=manual_prompt_text(manual))
            else:
                drafted = llm_mod.draft(args.name, d.full_text, "", lang=primary,
                                        web_text=web.text if web else "",
                                        manual_text=manual_prompt_text(manual),
                                        source_kind="record", identity=d.identity)
            if drafted is None:
                llm_note = "Sections could not be drafted (the local model returned nothing usable). Please write them by hand."
                print("! Draft failed — the YAML files are still written")
            else:
                llm_ok = True
                print("✓ Draft written")

    # 5. Ground and assemble the drafted language.
    data = {primary: build_yaml(name=args.name, slug=slug, drafted=drafted, deck_obj=d,
                                images=images, url=url, url_ok=url_ok,
                                llm_note=llm_note, lang=primary,
                                website_text=extra, web=web, manual=manual, logo=logo,
                                logo_source=logo_source, logo_note=logo_note, research_=res)}

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


class _RecordSource:
    """
    Stands in for a parsed deck when the one-pager is built from a database
    record: `full_text` is everything research.py gathered except the web
    search (passed to the model separately, and its figures flagged as
    third-party), so the grounding checks work unchanged.
    """
    kind = "record"

    def __init__(self, rec: dict, res: "research.Research"):
        self.rec, self.res = rec, res
        self.path = Path(f"{rec.get('name')}.record.json")
        self.slides = []
        self.full_text = research.material(rec, res, with_web=False)
        bits = [rec.get("short_description") or "", rec.get("city") or "",
                f"website {res.website}" if res.website else ""]
        self.identity = " — ".join(b for b in bits if b)

    def source_lines(self) -> list:
        rec, res = self.rec, self.res
        lines = [f"HubDrive database record: {rec.get('name')} (id {rec.get('id')}) — used as a lead only"]
        if res.article_url:
            lines.append(f"Source article: {res.article_url}")
        if res.site and res.site.ok:
            lines.append("Website pages read: " + ", ".join(u for u, _ in res.site.pages))
        return lines


def _read_existing(paths: dict, legacy: Path) -> dict:
    """The current draft (German first), to carry your input and website over."""
    for p in (paths[i18n.FINAL_LANG], paths[i18n.other(i18n.FINAL_LANG)], legacy):
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                return {}
    return {}


def _save_deck(deck: Path, slug: str) -> None:
    """Keep the deck so the dashboard can regenerate with new instructions."""
    src = deck.expanduser().resolve()
    dest = DECKS_DIR / f"{slug}{src.suffix.lower()}"
    if src == dest.resolve():
        return
    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    for other in DECKS_DIR.glob(f"{slug}.*"):
        other.unlink()
    shutil.copy2(src, dest)


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
