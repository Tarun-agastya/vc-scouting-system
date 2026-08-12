"""
Gmail IMAP/SMTP via App Password (Phase PM, 12 Aug 2026) — pure-logic tests.
No real network/IMAP/SMTP connection: gmail_auth's connection functions and
newsletter_ingestor's IMAP calls are mocked at the imaplib/smtplib boundary,
matching this project's existing pattern for the Gmail-API-era tests.
"""
import email
import imaplib
from email.mime.text import MIMEText

import pytest


# ── gmail_auth: credential validation ────────────────────────────────────────

def test_missing_credentials_raise_a_clear_error(monkeypatch):
    from ingestion import gmail_auth
    monkeypatch.setattr("config.settings.gmail_address", None)
    monkeypatch.setattr("config.settings.gmail_app_password", None)
    with pytest.raises(RuntimeError, match="GMAIL_ADDRESS"):
        gmail_auth.get_imap_connection()
    with pytest.raises(RuntimeError, match="GMAIL_ADDRESS"):
        gmail_auth.get_smtp_connection()


def test_imap_login_failure_gives_an_actionable_message(monkeypatch):
    """A wrong address/password shouldn't just bubble up imaplib's terse
    error -- confirmed live 12 Aug that a misdiagnosed auth failure (a typo,
    see this module's docstring) cost real debugging time once already."""
    from ingestion import gmail_auth

    monkeypatch.setattr("config.settings.gmail_address", "wrong@gmail.com")
    monkeypatch.setattr("config.settings.gmail_app_password", "badpassword")
    monkeypatch.setattr("config.settings.gmail_imap_host", "imap.gmail.com")
    monkeypatch.setattr("config.settings.gmail_imap_port", 993)

    class _FakeIMAP:
        def __init__(self, host, port):
            pass

        def login(self, user, password):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
    with pytest.raises(RuntimeError, match="typo"):
        gmail_auth.get_imap_connection()


# ── newsletter_ingestor: header decoding ────────────────────────────────────

def test_rfc2047_encoded_subject_is_decoded():
    """Raw IMAP fetch keeps non-ASCII subjects MIME-encoded, unlike the old
    Gmail API which decoded them server-side -- confirmed the emoji subject
    line this module's docstring references would otherwise reach
    candidate_filter as literal '=?UTF-8?B?...?=' garbage."""
    from ingestion.newsletter_ingestor import _decode_header_value

    msg = MIMEText("body")
    msg["Subject"] = "🟣 Milliardenrechnung von AWS"
    decoded = _decode_header_value(msg["Subject"])
    assert "Milliardenrechnung" in decoded
    assert "🟣" in decoded


def test_plain_ascii_subject_passes_through():
    from ingestion.newsletter_ingestor import _decode_header_value
    assert _decode_header_value("Weekly Startup Digest") == "Weekly Startup Digest"


def test_empty_header_returns_empty_string():
    from ingestion.newsletter_ingestor import _decode_header_value
    assert _decode_header_value(None) == ""
    assert _decode_header_value("") == ""


# ── newsletter_ingestor: text extraction from a real parsed message ────────

def test_extract_text_prefers_plain_over_html():
    from ingestion.newsletter_ingestor import NewsletterIngestor
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("Plain text version", "plain", "utf-8"))
    msg.attach(MIMEText("<p>HTML version</p>", "html", "utf-8"))

    ing = NewsletterIngestor()
    text = ing._extract_text(msg)
    assert "Plain text version" in text


def test_extract_text_falls_back_to_html_when_no_plain_part():
    from ingestion.newsletter_ingestor import NewsletterIngestor

    msg = MIMEText("<p>Only HTML <b>here</b></p>", "html", "utf-8")
    ing = NewsletterIngestor()
    text = ing._extract_text(msg)
    assert "Only HTML" in text
    assert "<p>" not in text  # stripped, not raw markup


def test_extract_text_handles_non_utf8_charset():
    from ingestion.newsletter_ingestor import NewsletterIngestor

    msg = email.message.Message()
    msg["Content-Type"] = 'text/plain; charset="latin-1"'
    msg.set_payload("caf\xe9 gr\xfcnder".encode("latin-1"))

    ing = NewsletterIngestor()
    text = ing._extract_text(msg)
    assert "caf" in text and "gr" in text  # decoded without raising


# ── newsletter_ingestor: UID listing against a mocked IMAP connection ──────

def test_list_all_uids_parses_search_response():
    from ingestion.newsletter_ingestor import NewsletterIngestor

    class _FakeConn:
        def search(self, charset, criterion):
            assert "SINCE" in criterion
            return ("OK", [b"12 15 19"])

    ing = NewsletterIngestor()
    ing._conn = _FakeConn()
    uids = ing._list_all_uids(days=14)
    assert uids == ["12", "15", "19"]


def test_list_all_uids_handles_no_matches():
    from ingestion.newsletter_ingestor import NewsletterIngestor

    class _FakeConn:
        def search(self, charset, criterion):
            return ("OK", [b""])

    ing = NewsletterIngestor()
    ing._conn = _FakeConn()
    assert ing._list_all_uids(days=14) == []


# ── newsletter_ingestor: UIDVALIDITY-aware state ────────────────────────────

def test_state_reset_when_uidvalidity_changes(tmp_path, monkeypatch):
    """Old processed-UID lists must not be trusted after Gmail bumps
    UIDVALIDITY -- stale UIDs could silently point at different messages."""
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    state_path.write_text(json.dumps({"processed_ids": ["1", "2", "3"], "uidvalidity": "1000"}))
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))

    class _FakeConn:
        def response(self, code):
            assert code == "UIDVALIDITY"
            return ("UIDVALIDITY", [b"2000"])  # changed

    ing = mod.NewsletterIngestor()
    ing._conn = _FakeConn()
    state = ing._load_state()
    assert state["processed_ids"] == []
    assert state["uidvalidity"] == "2000"


def test_state_preserved_when_uidvalidity_unchanged(tmp_path, monkeypatch):
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    state_path.write_text(json.dumps({"processed_ids": ["1", "2", "3"], "uidvalidity": "1000"}))
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))

    class _FakeConn:
        def response(self, code):
            return ("UIDVALIDITY", [b"1000"])  # unchanged

    ing = mod.NewsletterIngestor()
    ing._conn = _FakeConn()
    state = ing._load_state()
    assert state["processed_ids"] == ["1", "2", "3"]
