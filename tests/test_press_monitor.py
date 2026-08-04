"""
Press monitor (Phase PM, 4 Aug 2026) — pure-logic tests, no network/Ollama.
The live e-paper login/download/scan/summarize pipeline was verified
against a real edition on 4 Aug 2026 (see press_monitor/README.md); this
file locks in the parts that don't need a live subscriber session.
"""
import pytest

from press_monitor import scanner


def test_load_keywords_includes_known_terms():
    keywords = scanner.load_keywords()
    for expected in ("GreenTech Hub", "Kutter", "SÜDPACK", "HyWin", "AVIA"):
        assert expected in keywords


def _make_pdf(pages: list, path) -> None:
    """pages: list of plain-text page bodies."""
    import fitz
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 50), text, fontsize=11)
    doc.save(str(path))
    doc.close()


def test_scan_pdf_finds_matches_and_renders_screenshots(tmp_path):
    pdf_path = tmp_path / "edition.pdf"
    _make_pdf(
        [
            "Nothing relevant on this page, just local weather news.",
            "Die Firma Kutter feiert heute ihr Jubilaeum in Memmingen.",
        ],
        pdf_path,
    )
    out_dir = tmp_path / "pages"
    matches = scanner.scan_pdf(pdf_path, out_dir=out_dir, keywords=["Kutter", "SÜDPACK"])

    assert len(matches) == 1
    assert matches[0].page_number == 2
    assert matches[0].terms == ["Kutter"]
    assert matches[0].screenshot_path.exists()
    assert "Kutter" in matches[0].excerpt


def test_scan_pdf_no_matches_returns_empty(tmp_path):
    pdf_path = tmp_path / "edition.pdf"
    _make_pdf(["Nothing here about any watched company."], pdf_path)
    matches = scanner.scan_pdf(pdf_path, out_dir=tmp_path / "pages", keywords=["Kutter"])
    assert matches == []


def test_scan_pdf_multiple_terms_on_one_page(tmp_path):
    pdf_path = tmp_path / "edition.pdf"
    _make_pdf(["Kutter und Baufritz feiern gemeinsam einen Erfolg."], pdf_path)
    matches = scanner.scan_pdf(pdf_path, out_dir=tmp_path / "pages", keywords=["Kutter", "Baufritz"])
    assert len(matches) == 1
    assert set(matches[0].terms) == {"Kutter", "Baufritz"}


def test_two_separate_articles_on_one_page_produce_two_matches(tmp_path):
    """
    Correctness fix (4 Aug, after owner review): a page with two unrelated
    articles matching two different keywords must produce two separate
    Match entries, each with its OWN correct excerpt/screenshot — not one
    Match whose excerpt/screenshot only covers the first term's article
    while silently mis-attributing the second term to it.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Kutter feiert Jubilaeum heute in Memmingen.", fontsize=11)
    page.insert_text((50, 700), "SUEDPACK expandiert mit neuer Anlage in Ochsenhausen.", fontsize=11)
    pdf_path = tmp_path / "edition.pdf"
    doc.save(str(pdf_path))
    doc.close()

    matches = scanner.scan_pdf(pdf_path, out_dir=tmp_path / "pages", keywords=["Kutter", "SUEDPACK"])

    assert len(matches) == 2
    by_term = {m.terms[0]: m for m in matches}
    assert set(by_term.keys()) == {"Kutter", "SUEDPACK"}
    # Each match's excerpt must contain ONLY its own article's content, not
    # the other one's — this is the exact cross-contamination the fix
    # addresses.
    assert "Kutter" in by_term["Kutter"].excerpt
    assert "SUEDPACK" not in by_term["Kutter"].excerpt
    assert "SUEDPACK" in by_term["SUEDPACK"].excerpt
    assert "Kutter" not in by_term["SUEDPACK"].excerpt
    # Distinct screenshot files, both real.
    assert by_term["Kutter"].screenshot_path != by_term["SUEDPACK"].screenshot_path
    assert by_term["Kutter"].screenshot_path.exists()
    assert by_term["SUEDPACK"].screenshot_path.exists()


def test_screenshot_is_cropped_not_full_page(tmp_path):
    """
    Phase PM crop fix (4 Aug, after owner feedback): the screenshot must be
    the matched article's own region, not the whole broadsheet page — a
    cropped render at a small headline+body layout should be visibly
    smaller than rendering the full page at the same zoom.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4-ish broadsheet proxy
    page.insert_text((50, 50), "Kutter feiert Jubilaeum", fontsize=16)
    page.insert_text((50, 80), "Die Firma Kutter feiert ihr 100-jaehriges Bestehen.", fontsize=11)
    page.insert_text((50, 700), "Ganz unten auf der Seite: unrelated other article.", fontsize=11)
    pdf_path = tmp_path / "edition.pdf"
    doc.save(str(pdf_path))
    doc.close()

    matches = scanner.scan_pdf(pdf_path, out_dir=tmp_path / "pages", keywords=["Kutter"])
    assert len(matches) == 1

    from PIL import Image
    img = Image.open(matches[0].screenshot_path)
    # A crop of a ~2-line headline+body region must be much shorter than a
    # full 842pt-tall page rendered at the same zoom (3x -> ~2526px tall).
    assert img.height < 600


def test_article_region_includes_headline_above_match(tmp_path):
    """The crop must walk upward to include a headline block sitting just
    above the block that actually contains the matched term."""
    import fitz
    from press_monitor.scanner import _article_region

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Kutter feiert 100 Jahre", fontsize=16)
    page.insert_text((50, 90), "Die Firma Kutter feiert heute ihr Jubilaeum in Memmingen.", fontsize=11)

    hits = page.search_for("Kutter feiert heute")
    assert hits, "search_for should find the body text"
    region = _article_region(page, hits[0])
    assert region is not None
    # The headline sits at y~50; the region must extend up to include it,
    # not start only at the body block's own top edge (~85-ish).
    assert region.y0 < 70


def test_emailer_sends_via_gmail_api(monkeypatch, tmp_path):
    """
    emailer.send_digest() now goes through the Gmail API (ingestion/
    gmail_auth.get_gmail_service), not SMTP — confirm it builds a message
    and calls .send() rather than exercising real OAuth/network.
    """
    from press_monitor import emailer, scanner

    sent = {}

    class _FakeMessages:
        def send(self, userId, body):
            sent["userId"] = userId
            sent["body"] = body
            class _Exec:
                def execute(self_inner):
                    return {"id": "fake-message-id"}
            return _Exec()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    monkeypatch.setattr("ingestion.gmail_auth.get_gmail_service", lambda: _FakeService())

    shot = tmp_path / "page_001.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG-ish bytes, content unused by the fake
    match = scanner.Match(page_number=1, terms=["Kutter"], excerpt="Kutter feiert...", screenshot_path=shot)
    match.summary = "Kutter feiert 100-jaehriges Jubilaeum."

    emailer.send_digest(matches=[match], edition_label="Test 04.08.2026", recipients=["a@b.com"])

    assert sent["userId"] == "me"
    assert "raw" in sent["body"]


def test_summarizer_falls_back_cleanly_on_llm_failure(monkeypatch):
    from press_monitor import summarizer

    class _FailingClient:
        def generate(self, *a, **kw):
            raise RuntimeError("Ollama unreachable")

    monkeypatch.setattr("reasoning.qwen_client.qwen_client", _FailingClient())
    result = summarizer.summarize_match("Kutter", "Die Firma Kutter feiert ihr Jubiläum.")
    assert "Kutter" in result  # excerpt-based fallback still surfaces the term
    assert "nicht verfügbar" in result


def test_summarizer_retries_once_on_empty_result_then_succeeds(monkeypatch):
    """
    Found live 4 Aug: a real call returned a completely empty string with
    NO exception raised — the identical prompt succeeded on immediate
    retry. summarize_match must never silently accept an empty "summary".
    """
    from press_monitor import summarizer

    calls = {"n": 0}

    class _FlakyOnceClient:
        def generate(self, *a, **kw):
            calls["n"] += 1
            return "" if calls["n"] == 1 else "Ein echter, ausreichend langer Zusammenfassungstext."

    monkeypatch.setattr("reasoning.qwen_client.qwen_client", _FlakyOnceClient())
    result = summarizer.summarize_match("Kutter", "Die Firma Kutter feiert ihr Jubiläum.")
    assert calls["n"] == 2
    assert result == "Ein echter, ausreichend langer Zusammenfassungstext."


def test_summarizer_falls_back_when_empty_twice_in_a_row(monkeypatch):
    from press_monitor import summarizer

    class _AlwaysEmptyClient:
        def generate(self, *a, **kw):
            return ""

    monkeypatch.setattr("reasoning.qwen_client.qwen_client", _AlwaysEmptyClient())
    result = summarizer.summarize_match("Kutter", "Die Firma Kutter feiert ihr Jubiläum.")
    assert "Kutter" in result
    assert "nicht verfügbar" in result
