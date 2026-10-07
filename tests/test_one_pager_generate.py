"""
One-pager generator — deck parsing, grounding, and YAML assembly.

Pure and hermetic on purpose: no Ollama, no network, no Postgres, no Qdrant.
Real PDF and PPTX files are built in-test with fitz and python-pptx (both
already dependencies) rather than committed as fixtures, so the tests exercise
the actual parsers against actual files while staying self-contained. That
also means these pass with the whole scouting stack switched off — the
condition the isolation exists for.
"""
import sys
from pathlib import Path

import pytest

ONE_PAGER_DIR = Path(__file__).resolve().parent.parent / "templates" / "one_pager"
sys.path.insert(0, str(ONE_PAGER_DIR))

import deck as deck_mod            # noqa: E402
import generate as gen             # noqa: E402
import llm as llm_mod              # noqa: E402


# ── fixtures: real files, built on the fly ───────────────────────────────────

SLIDES = [
    "Problem",                                    # chapter divider — not content
    "Schwankende Dieselkosten belasten Landwirte zunehmend und 65.000 Betriebe "
    "erzeugen bereits eigenen Strom.",
    "Der ONOX 1 ist ein vollelektrischer Traktor mit Wechselmodulen, der ohne "
    "Standzeiten beim Laden den ganzen Tag arbeitet.",
    "Gegruendet 2021 in Isny. Team: 11 Personen. 11t CO2 Einsparung pro Jahr.",
]


@pytest.fixture
def pdf_deck(tmp_path):
    import fitz
    doc = fitz.open()
    for text in SLIDES:
        page = doc.new_page()
        y = 90
        for line in text.split(". "):
            page.insert_text((60, y), line, fontsize=11)
            y += 18
    path = tmp_path / "deck.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def pptx_deck(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    for text in SLIDES:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(3))
        box.text_frame.text = text
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    return path


# ── parsing: both formats reach the same shape ───────────────────────────────

def test_pdf_and_pptx_parse_to_the_same_shape(pdf_deck, pptx_deck):
    a, b = deck_mod.parse_deck(pdf_deck), deck_mod.parse_deck(pptx_deck)
    assert (a.kind, b.kind) == ("pdf", "pptx")
    assert len(a.slides) == len(b.slides) == len(SLIDES)
    for d in (a, b):
        assert "ONOX 1" in d.full_text
        assert "[Folie 3]" in d.full_text          # slide numbers survive, for citations


def test_chapter_dividers_are_not_treated_as_content(pdf_deck):
    d = deck_mod.parse_deck(pdf_deck)
    assert d.slides[0].text.strip() == "Problem"
    assert d.slides[0].is_content is False, "a one-word chapter slide is not content"
    assert d.slides[1].is_content is True


def test_content_text_respects_its_budget(pdf_deck):
    d = deck_mod.parse_deck(pdf_deck)
    assert len(d.content_text(120)) <= 200          # budget honoured (+ slide headers)
    assert d.content_text(5000)                     # a generous budget still returns text


def test_unreadable_and_unsupported_decks_fail_clearly(tmp_path):
    with pytest.raises(deck_mod.DeckError, match="not found"):
        deck_mod.parse_deck(tmp_path / "nope.pdf")

    legacy = tmp_path / "old.ppt"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(deck_mod.DeckError, match="re-save as .pptx|legacy"):
        deck_mod.parse_deck(legacy)

    other = tmp_path / "x.key"
    other.write_bytes(b"x")
    with pytest.raises(deck_mod.DeckError, match="unsupported"):
        deck_mod.parse_deck(other)


def test_image_extraction_never_raises_and_names_by_slide(pdf_deck, tmp_path):
    d = deck_mod.parse_deck(pdf_deck)
    names = deck_mod.extract_images(d, tmp_path / "assets")
    assert names, "a PDF should always yield at least page renders"
    assert all(n.startswith("slide") for n in names)
    assert any("_page" in n for n in names)


# ── grounding ────────────────────────────────────────────────────────────────

SOURCE = ("11t CO2 pro Jahr. Gesamtkosten nach 8 Jahren: ONOX 95.861 EUR gegenueber "
          "Verbrenner 121.300 EUR. 600 Newsletter Abos, 25 Probefahrten. Team: 11 Personen.")


def test_grounding_catches_a_number_the_model_computed_itself():
    """
    The real case, from the first live run against an ONOX-shaped deck: given
    121.300 and 95.861, the model wrote "25.439 EUR gesenkt" — arithmetically
    correct, but that figure appears nowhere in the source. A reader would
    assume it came from the deck.
    """
    assert gen._unsupported_numbers("Nach 8 Jahren 25.439 EUR gesenkt.", SOURCE) == ["25.439"]


def test_grounding_passes_numbers_that_are_really_in_the_deck():
    assert gen._unsupported_numbers("11t CO2, 95.861 EUR nach 8 Jahren.", SOURCE) == []


def test_grounding_ignores_thousand_separator_style():
    """95861 and 95.861 are the same number — a format difference must not
    read as a fabrication."""
    assert gen._unsupported_numbers("Kosten 95861 EUR.", SOURCE) == []


def test_grounding_never_touches_prose_without_numbers():
    assert gen._unsupported_numbers("Ein vollelektrischer Traktor für Höfe.", SOURCE) == []


def test_unsupported_team_size_becomes_none_not_a_guess():
    assert gen._supported_meta("11", SOURCE) == "11"
    assert gen._supported_meta("6", SOURCE) is None     # -> rendered as "k. A."
    assert gen._supported_meta(None, SOURCE) is None


# ── YAML assembly ────────────────────────────────────────────────────────────

def _build(pdf_deck, drafted, images=("slide01_page.jpg",), llm_note=None, lang="de"):
    return gen.build_yaml(
        name="ONOX", slug="onox", drafted=drafted,
        deck_obj=deck_mod.parse_deck(pdf_deck), images=list(images),
        url=None, url_ok=False, llm_note=llm_note, lang=lang,
    )


def test_yaml_has_every_field_the_renderer_requires(pdf_deck):
    data = _build(pdf_deck, {
        "claim": "Elektrischer Traktor mit Wechselbatterien",
        "location": "Isny", "founded": "2021", "team_size": "11",
        "loesung": "Ein Traktor.", "mehrwerte": "11t CO2 pro Jahr.",
        "usp": "Wechselmodule.", "zielgruppe": "Höfe.", "geschaeftsmodell": "Verkauf.",
    })
    for key in ("claim", "name", "location", "founded", "team_size", "sections", "visuals"):
        assert key in data
    assert set(data["sections"]) == set(gen.SECTION_LABELS)
    assert data["review"]["status"] == "draft", "generated pages are never pre-approved"


def test_a_failed_draft_still_produces_a_complete_yaml(pdf_deck):
    """Ollama being down must cost the prose, never the run."""
    data = _build(pdf_deck, None, llm_note="Ollama nicht erreichbar")
    assert data["claim"] == ""
    assert all(v == "" for v in data["sections"].values())
    assert data["location"] == "k. A."
    q = " ".join(data["review"]["open_questions"])
    assert "Ollama" in q
    for heading in gen.SECTION_LABELS.values():
        assert heading in q, f"the human must be told '{heading}' needs writing"


def test_missing_images_are_flagged_rather_than_faked(pdf_deck):
    data = _build(pdf_deck, None, images=())
    assert "placeholder" in data["visuals"]["visual_solution"]
    assert "image" not in data["visuals"]["visual_solution"]
    assert any("no images" in q.lower() for q in data["review"]["open_questions"])


def test_sources_cite_the_deck_and_the_slides_used(pdf_deck):
    data = _build(pdf_deck, None)
    joined = " ".join(data["sources"])
    assert "deck.pdf" in joined and "slides" in joined
    assert "Content slides used" in joined


# ── llm helpers (no network) ─────────────────────────────────────────────────

def test_llm_normalise_strips_sentinels_claim_period_and_team_noise():
    out = llm_mod._normalise({
        "claim": "Elektrischer Traktor mit Wechselbatterien.",   # FORMAT.md forbids the period
        "location": "Isny", "founded": "2021", "team_size": "11 Personen",
        "loesung": "Ein Traktor.", "mehrwerte": "", "usp": "   ",
        "zielgruppe": "Höfe.", "geschaeftsmodell": "Verkauf.",
    })
    assert out["claim"] == "Elektrischer Traktor mit Wechselbatterien"
    assert out["team_size"] == "11"
    assert out["mehrwerte"] is None and out["usp"] is None   # "" means "deck doesn't say"


def test_llm_draft_returns_none_when_ollama_is_unreachable(monkeypatch):
    monkeypatch.setattr(llm_mod, "BASE_URL", "http://localhost:1")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)   # don't pay the retry pause
    assert llm_mod.draft("X", "[Folie 1]\nEin Traktor.") is None


def test_slugify_handles_umlauts_and_punctuation():
    assert gen.slugify("Hula Earth") == "hula_earth"
    assert gen.slugify("Müller & Söhne GmbH") == "mueller_soehne_gmbh"
    assert gen.slugify("!!!") == "startup"


# ── Two languages: German (final) + English ──────────────────────────────────

import i18n                        # noqa: E402
import render as render_mod        # noqa: E402

_DRAFT_DE = {
    "claim": "Elektrischer Traktor mit Wechselbatterien",
    "location": "Isny", "founded": "2021", "team_size": "11",
    "loesung": "Ein Traktor.", "mehrwerte": "11t CO2 pro Jahr.",
    "usp": "Wechselmodule.", "zielgruppe": "Höfe.", "geschaeftsmodell": "Verkauf.",
}
_TRANSLATED_EN = {
    "claim": "Electric tractor with swappable batteries",
    "location": "Isny", "founded": "2021", "team_size": "11",
    "loesung": "A tractor.", "mehrwerte": "11t CO2 per year.",
    "usp": "Swappable modules.", "zielgruppe": "Farms.", "geschaeftsmodell": "Sales.",
}


def test_german_is_the_final_version_and_drafted_from_the_deck():
    assert i18n.FINAL_LANG == "de"
    assert gen.yaml_path("onox", "de").name == "onox.de.yaml"
    assert gen.yaml_path("onox", "en").name == "onox.en.yaml"


def test_each_file_carries_its_language_and_unknown_marker(pdf_deck):
    de = _build(pdf_deck, None, lang="de")
    en = _build(pdf_deck, None, lang="en")
    assert de["lang"] == "de" and de["location"] == "k. A."
    assert en["lang"] == "en" and en["location"] == "n/a"
    assert de["visuals"]["visual_solution"]["label"] == "Visualisierung der Lösung"
    assert en["visuals"]["visual_solution"]["label"] == "Visualisation of the Solution"


def test_translation_keeps_images_and_facts_and_swaps_labels(pdf_deck, monkeypatch):
    de = _build(pdf_deck, _DRAFT_DE, lang="de")
    de["visuals"]["visual_solution"]["image"] = "assets/onox/slide01_page.jpg"
    monkeypatch.setattr(gen.llm_mod, "translate", lambda f, s, d: dict(_TRANSLATED_EN))
    en, ok = gen.translate_data(de, "en")
    assert ok and en["lang"] == "en"
    assert en["claim"] == _TRANSLATED_EN["claim"]
    assert en["sections"]["usp"] == "Swappable modules."
    assert en["visuals"]["visual_solution"]["image"] == "assets/onox/slide01_page.jpg"
    assert en["visuals"]["visual_solution"]["label"] == "Visualisation of the Solution"
    assert en["meta"]["page_label"] == "Matchmaking Startups"
    assert en["sources"] == de["sources"]
    assert en["review"]["status"] == "draft"
    assert any("Translated automatically" in q for q in en["review"]["open_questions"])
    assert de["lang"] == "de", "the source must not be mutated"


def test_translation_that_invents_a_number_is_flagged(pdf_deck, monkeypatch):
    de = _build(pdf_deck, _DRAFT_DE, lang="de")
    bad = dict(_TRANSLATED_EN, mehrwerte="12t CO2 per year.")
    monkeypatch.setattr(gen.llm_mod, "translate", lambda f, s, d: bad)
    en, ok = gen.translate_data(de, "en")
    assert any("12" in q and "Benefits" in q for q in en["review"]["open_questions"])


def test_translation_can_never_change_team_size_or_year(pdf_deck, monkeypatch):
    de = _build(pdf_deck, _DRAFT_DE, lang="de")
    de["team_size"] = "4"
    monkeypatch.setattr(gen.llm_mod, "translate",
                        lambda f, s, d: dict(_TRANSLATED_EN, team_size="40", founded="2012"))
    en, _ = gen.translate_data(de, "en")
    assert en["team_size"] == "4" and en["founded"] == de["founded"]


def test_failed_translation_still_writes_a_complete_twin(pdf_deck, monkeypatch):
    de = _build(pdf_deck, _DRAFT_DE, lang="de")
    monkeypatch.setattr(gen.llm_mod, "translate", lambda f, s, d: None)
    en, ok = gen.translate_data(de, "en")
    assert not ok
    assert all(v == "" for v in en["sections"].values())
    assert any("Translation from the Deutsch version failed" in q for q in en["review"]["open_questions"])
    assert en["location"] == "Isny", "an untranslated fact beats an empty one"


def test_unknown_marker_maps_between_languages(pdf_deck, monkeypatch):
    de = _build(pdf_deck, dict(_DRAFT_DE, team_size=None), lang="de")
    assert de["team_size"] == "k. A."
    monkeypatch.setattr(gen.llm_mod, "translate", lambda f, s, d: dict(_TRANSLATED_EN, team_size=None))
    en, _ = gen.translate_data(de, "en")
    assert en["team_size"] == "n/a"


@pytest.mark.parametrize("lang,headings,meta", [
    ("de", ["Lösung & Funktionalität", "Geschäftsmodell"], "Gründung: 2021"),
    ("en", ["Solution & Functionality", "Business Model"], "Founded: 2021"),
])
def test_render_uses_the_files_language_throughout(pdf_deck, tmp_path, lang, headings, meta):
    data = _build(pdf_deck, _DRAFT_DE, lang=lang)
    assert render_mod.validate(data, tmp_path / "x.yaml") == []
    page = render_mod.render(data, tmp_path)
    import html
    for h in headings:
        assert html.escape(h) in page
    assert meta in page and f'lang="{lang}"' in page
    other = "en" if lang == "de" else "de"
    assert html.escape(i18n.labels(other)["sections"]["usp"]) not in page, "a page must never mix languages"


def test_a_file_without_lang_is_german(pdf_deck, tmp_path):
    data = _build(pdf_deck, _DRAFT_DE, lang="de")
    data.pop("lang")
    assert "Lösung &amp; Funktionalität" in render_mod.render(data, tmp_path)


def test_pptx_export_uses_the_files_language(pdf_deck, tmp_path):
    import export_pptx
    data = _build(pdf_deck, _DRAFT_DE, lang="en")
    prs = export_pptx.new_deck()
    export_pptx.build_slide(prs, data, tmp_path, tmp_path / "tmp")
    text = " ".join(sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame)
    assert "Business Model" in text and "Founded: 2021" in text
    assert "Geschäftsmodell" not in text


# ── Draft quality: the whole deck reaches the model ──────────────────────────

def _footer_deck(tmp_path, n_slides=12):
    """A deck shaped like Eidola's: a numbered footer on every slide, one long
    bio slide, and short slides that carry the actual argument."""
    import fitz
    doc = fitz.open()
    footer = "ACME: Die Rueckfuehrung mineralischer Nebenprodukte in die Kreislaufwirtschaft."
    for i in range(1, n_slides + 1):
        body = ("Gruenderin mit langem Lebenslauf. " * 120) if i == 2 else f"Kernaussage {i}: das Problem, die Loesung und der Markt in einem Satz."
        page = doc.new_page(width=1600, height=2400)
        page.insert_textbox(fitz.Rect(20, 20, 1580, 2380), f"{i} {footer}\n{body}", fontsize=9)
    path = tmp_path / "footer.pdf"
    doc.save(path)
    return deck_mod.parse_deck(path)


def test_every_slide_reaches_the_model_and_long_slides_are_trimmed(tmp_path):
    d = _footer_deck(tmp_path)
    text = d.content_text(2000)
    assert len(text) <= 2000
    for i in range(1, 13):
        assert f"[Folie {i}]" in text, f"slide {i} was dropped"
        if i != 2:
            assert f"Kernaussage {i}" in text, "a short slide must keep all of its text"


def test_a_repeated_numbered_footer_is_removed(tmp_path):
    d = _footer_deck(tmp_path)
    assert "Kreislaufwirtschaft" not in d.content_text(14000)


def test_short_sections_and_a_figureless_benefit_are_flagged(pdf_deck):
    data = _build(pdf_deck, dict(_DRAFT_DE, loesung="Ein Traktor.", mehrwerte="Spart viel Diesel."))
    q = " ".join(data["review"]["open_questions"])
    assert "'Solution & Functionality' is only 2 words" in q
    assert "'Benefits & Performance' has no concrete figure" in q


def test_drafting_prompt_carries_no_other_companys_facts():
    """An example page in the prompt leaked 'bis zu 90 %' from LIGARO into
    Eidola's draft. No other startup's page may sit in the drafting prompt."""
    for lang, (system, prompt) in llm_mod._PROMPTS.items():
        for leak in ("LIGARO", "Henkel", "EGGER", "200.000", "200,000"):
            assert leak not in system + prompt, f"{lang} prompt contains {leak!r}"


def test_preview_renders_an_incomplete_draft_but_export_still_refuses(pdf_deck, tmp_path):
    import subprocess, sys, yaml
    data = _build(pdf_deck, dict(_DRAFT_DE, mehrwerte="Spart viel Diesel."))
    src = tmp_path / "x.de.yaml"
    src.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    tool = Path(gen.__file__).parent
    run = lambda *a: subprocess.run([sys.executable, *a], capture_output=True, text=True)
    ok = run(str(tool / "render.py"), str(src), "--allow-incomplete", "--out-dir", str(tmp_path))
    assert ok.returncode == 0 and (tmp_path / "x.de_onepager.html").exists()
    assert "ENTWURF" in (tmp_path / "x.de_onepager.html").read_text(encoding="utf-8")
    assert run(str(tool / "render.py"), str(src), "--check").returncode == 1
    assert run(str(tool / "export_pptx.py"), str(src), "--out-dir", str(tmp_path)).returncode == 1


# ── Your input, website, logo, team approximations ────────────────────────────

def test_team_size_keeps_ranges_and_approximations():
    assert llm_mod.normalise_team("6 Personen", "de") == "6"
    assert llm_mod.normalise_team("11-50 employees", "en") == "11–50"
    assert llm_mod.normalise_team("about 10 people", "en") == "approx. 10"
    assert llm_mod.normalise_team("rund 10 Mitarbeitende", "de") == "ca. 10"
    assert llm_mod.normalise_team("über 20", "de") == "20+"


def test_a_team_range_is_supported_only_if_both_ends_are_in_a_source():
    assert gen._supported_meta("11–50", "LinkedIn: 11-50 employees") == "11–50"
    assert gen._supported_meta("11–50", "we are 11 people") is None
    assert gen._supported_meta("ca. 10", "rund 10 Personen") == "ca. 10"


def test_your_input_beats_the_sources_and_is_never_flagged(pdf_deck):
    manual = {"team_size": "12", "location": "Kempten",
              "notes": "Pilot mit 3 Allgäuer Molkereien seit 2026."}
    drafted = dict(_DRAFT_DE, team_size="99", location="Isny",
                   zielgruppe="Höfe und 3 Allgäuer Molkereien seit 2026.")
    data = gen.build_yaml(
        name="ONOX", slug="onox", drafted=drafted, deck_obj=deck_mod.parse_deck(pdf_deck),
        images=["slide01_page.jpg"], url=None, url_ok=False, llm_note=None, lang="de",
        manual=manual)
    assert data["team_size"] == "12" and data["location"] == "Kempten"
    assert data["manual"] == manual
    q = " ".join(data["review"]["open_questions"])
    assert "Team size" not in q and "Location not found" not in q
    assert "2026" not in q and "'3'" not in q, "figures from your input are a source, not a fabrication"


def test_an_unsupported_team_estimate_is_shown_to_you_not_printed(pdf_deck):
    data = _build(pdf_deck, dict(_DRAFT_DE, team_size="ca. 7"), lang="de")
    assert data["team_size"] == "k. A."
    assert any("estimated 'ca. 7'" in q and "Team size" in q for q in data["review"]["open_questions"])


def test_the_model_is_given_your_input_first():
    text = gen.manual_prompt_text({"team_size": "12", "notes": "Betone das Lizenzmodell."})
    assert "Team size / Teamgröße: 12" in text and "Betone das Lizenzmodell." in text
    for _lang, (_system, prompt) in llm_mod._PROMPTS.items():
        assert "[GT Hub input]" in prompt


def test_translation_keeps_your_values_verbatim(pdf_deck, monkeypatch):
    de = gen.build_yaml(name="ONOX", slug="onox", drafted=_DRAFT_DE, deck_obj=deck_mod.parse_deck(pdf_deck),
                        images=[], url=None, url_ok=False, llm_note=None, lang="de",
                        manual={"location": "München", "team_size": "ca. 12"})
    monkeypatch.setattr(gen.llm_mod, "translate",
                        lambda f, s, d: dict(_TRANSLATED_EN, location="Munich", team_size="approx. 12"))
    en, _ = gen.translate_data(de, "en")
    assert en["location"] == "München" and en["team_size"] == "ca. 12"


def test_website_and_logo_reach_the_yaml_and_the_page(pdf_deck, tmp_path):
    from PIL import Image
    (tmp_path / "assets" / "onox").mkdir(parents=True)
    Image.new("RGBA", (300, 100), (0, 0, 0, 255)).save(tmp_path / "assets" / "onox" / "logo.png")
    data = gen.build_yaml(name="ONOX", slug="onox", drafted=_DRAFT_DE, deck_obj=deck_mod.parse_deck(pdf_deck),
                          images=[], url="https://www.onox.de/", url_ok=True, llm_note=None, lang="de",
                          logo="assets/onox/logo.png", logo_source="the website's apple-touch-icon")
    assert data["website"] == "https://www.onox.de/" and data["logo"] == "assets/onox/logo.png"
    assert any("Logo taken automatically" in q for q in data["review"]["open_questions"])
    page = render_mod.render(data, tmp_path, embed=True)
    assert 'href="https://www.onox.de/"' in page and ">onox.de</a>" in page
    assert "data:image/png;base64" in page
    assert 'aria-label="GT Hub"' in page, "the GT Hub lockup (logo + name) is on every page"


def test_no_logo_is_flagged_and_gets_an_initials_tile(pdf_deck, tmp_path):
    data = _build(pdf_deck, _DRAFT_DE, lang="de")
    assert "logo" not in data
    assert any(q.startswith("No logo") for q in data["review"]["open_questions"])
    assert '>O</div>' in render_mod.render(data, tmp_path)


def test_page_follows_the_style_guide(pdf_deck, tmp_path):
    import brand
    page = render_mod.render(_build(pdf_deck, _DRAFT_DE, lang="de"), tmp_path, embed=True)
    for token in (brand.LIME, brand.PURPLE, "font-family: 'Work Sans'", "font/ttf;base64"):
        assert token in page
    assert "#6C5CE7" not in page, "the old non-brand violet is gone"
    assert brand.corner_radius(1080) == 45, "style guide p.9's worked example"


def test_regeneration_keeps_your_input_until_you_replace_it(pdf_deck, tmp_path, monkeypatch):
    """The CLI end to end, with no model and no network: input given once is
    still there after a plain --force, and --manual-replace clears it."""
    import yaml
    from PIL import Image
    monkeypatch.setattr(gen, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gen, "ASSETS_DIR", tmp_path / "assets")
    monkeypatch.setattr(gen, "DECKS_DIR", tmp_path / "decks")
    logo = tmp_path / "mine.png"
    Image.new("RGB", (120, 120), (215, 241, 89)).save(logo)

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["generate.py", "--deck", str(pdf_deck), "--name", "ONOX",
                                          "--no-llm", "--no-web-search", "--save-deck", *extra])
        assert gen.main() == 0
        return yaml.safe_load((tmp_path / "onox.de.yaml").read_text(encoding="utf-8"))

    first = run("--team-size", "7", "--notes", "Betone den Service.", "--logo", str(logo))
    assert first["team_size"] == "7" and first["logo"] == "assets/onox/logo_manual.png"
    assert (tmp_path / "decks" / "onox.pdf").exists(), "deck kept for regeneration"
    again = run("--force")
    assert again["manual"]["team_size"] == "7" and again["manual"]["notes"] == "Betone den Service."
    assert again["logo"] == "assets/onox/logo_manual.png"
    cleared = run("--force", "--manual-replace", "--team-size", "")
    assert "team_size" not in (cleared.get("manual") or {}) and cleared["team_size"] == "k. A."
