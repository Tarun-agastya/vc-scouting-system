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


def test_emailer_raises_without_smtp_credentials(monkeypatch, tmp_path):
    from press_monitor import emailer
    from config import settings

    monkeypatch.setattr(settings, "smtp_user", None)
    monkeypatch.setattr(settings, "smtp_app_password", None)

    with pytest.raises(RuntimeError):
        emailer.send_digest(matches=[], edition_label="Test", recipients=["a@b.com"])


def test_summarizer_falls_back_cleanly_on_llm_failure(monkeypatch):
    from press_monitor import summarizer

    class _FailingClient:
        def generate(self, *a, **kw):
            raise RuntimeError("Ollama unreachable")

    monkeypatch.setattr("reasoning.qwen_client.qwen_client", _FailingClient())
    result = summarizer.summarize_match("Kutter", "Die Firma Kutter feiert ihr Jubiläum.")
    assert "Kutter" in result  # excerpt-based fallback still surfaces the term
    assert "nicht verfügbar" in result
