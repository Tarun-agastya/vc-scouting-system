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


def test_old_uid_state_file_is_migrated_not_trusted(tmp_path, monkeypatch):
    """
    Replaces two tests of the old UIDVALIDITY reset logic, removed 16 Aug 2026
    together with the UID keying they defended.

    The marker is now the RFC 5322 Message-ID, which is stable across a
    UIDVALIDITY bump, across the 12 Aug Gmail-API -> IMAP migration, and across
    a full re-download of the mailbox — so there is nothing left for a
    UIDVALIDITY check to protect against. What the loader must do instead is
    refuse to trust the old file: the live state file was found holding a MIX
    of 112 Gmail-API hex ids and 37 IMAP UIDs, neither of which is a
    Message-ID, and treating either as one would silently hide real mail.
    """
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    state_path.write_text(json.dumps({
        "processed_ids": ["66", "67", "19efef57c86302b1"],   # the real mixed shape
        "uidvalidity": "1",
    }))
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))

    state = mod.NewsletterIngestor()._load_state()
    assert state["processed_message_ids"] == [], "old UIDs/Gmail ids must never be read as Message-IDs"
    assert state["legacy_ids"] == ["66", "67", "19efef57c86302b1"], "kept for reference, not for matching"


def test_message_id_state_round_trips(tmp_path, monkeypatch):
    """A file already in the new format is loaded verbatim, and saving is atomic."""
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))
    ing = mod.NewsletterIngestor()

    ids = ["abc@mail.example", "def@newsletter.test"]
    ing._save_state({"processed_message_ids": ids, "legacy_ids": []})
    assert not (tmp_path / "newsletter_state.json.tmp").exists(), "temp file must be renamed away"

    loaded = ing._load_state()
    assert loaded["processed_message_ids"] == ids


def test_a_message_that_yielded_nothing_is_still_marked(tmp_path, monkeypatch):
    """
    Remembering "we looked and it had nothing" matters as much as remembering a
    successful extraction — otherwise every newsletter with no startups in it
    gets re-fetched and re-parsed by the LLM on every single run, forever.
    """
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))

    ing = mod.NewsletterIngestor()
    ing._conn = object()                                     # never used: everything below is stubbed
    monkeypatch.setattr(ing, "_authenticate", lambda: None)
    monkeypatch.setattr(ing, "_list_all_uids", lambda days: [b"7"])
    monkeypatch.setattr(ing, "_message_ids_for", lambda uids: {b"7": "empty@news.test"})
    monkeypatch.setattr(ing, "_process_message", lambda uid: 0)   # zero startups found

    assert ing.run_ingestion(max_messages=10, days=90) == 0
    assert json.loads(state_path.read_text())["processed_message_ids"] == ["empty@news.test"]


def test_already_marked_messages_are_never_fetched(tmp_path, monkeypatch):
    """The skip must happen before the expensive body fetch, not after."""
    import json
    from ingestion import newsletter_ingestor as mod

    state_path = tmp_path / "newsletter_state.json"
    state_path.write_text(json.dumps({
        "processed_message_ids": ["seen@news.test"], "legacy_ids": [],
    }))
    monkeypatch.setattr(mod, "_STATE_PATH", str(state_path))

    fetched = []
    ing = mod.NewsletterIngestor()
    ing._conn = object()
    monkeypatch.setattr(ing, "_authenticate", lambda: None)
    monkeypatch.setattr(ing, "_list_all_uids", lambda days: [b"1", b"2"])
    monkeypatch.setattr(ing, "_message_ids_for",
                        lambda uids: {b"1": "seen@news.test", b"2": "fresh@news.test"})
    monkeypatch.setattr(ing, "_process_message", lambda uid: fetched.append(uid) or 3)

    assert ing.run_ingestion(max_messages=10, days=90) == 3
    assert fetched == [b"2"], "the already-marked message must not be fetched at all"
