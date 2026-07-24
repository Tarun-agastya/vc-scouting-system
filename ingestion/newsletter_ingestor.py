import json
import os
import base64
import logging
from datetime import datetime
from typing import List, Optional
from bs4 import BeautifulSoup
from config import settings
from config.source_loader import get_newsletter_search_terms, get_newsletter_senders

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH   = os.path.join(_PROJECT_ROOT, "credentials", "token.json")
_STATE_PATH  = os.path.join(_PROJECT_ROOT, "credentials", "newsletter_state.json")


def _build_search_query(days: int = 14) -> str:
    """
    Build the Gmail search query. This is a dedicated scouting inbox, so by
    default every email in the window is fetched and relevance is filtered
    by content downstream (candidate_filter.is_relevant, per chunk) — not by
    guessing what words a newsletter's subject line contains.

    days controls the window: 14 for the routine scheduled top-up, a much
    larger value (see run_ingestion's `days` param) for an explicit backfill
    sweep of older mail the rolling window never reaches on its own — a real
    gap confirmed live 24 Jul: a 61-message mailbox had only its most recent
    15 messages inside the 14-day window, permanently missing the other 46
    (32 of which sit in the Promotions category) on every routine run.

    No date/category restriction is applied beyond `newer_than:{days}d` —
    Gmail's default search scope already covers every category tab
    (Primary/Promotions/Social/Updates), confirmed live: a plain
    `newer_than:14d` query returned the exact same message set as the union
    of per-category searches. The earlier suspicion that Promotions mail was
    being excluded by the query itself was wrong; the real cause was the
    14-day window never reaching older mail at all.

    A subject:(...) keyword restriction used to be applied unconditionally
    and silently dropped ~85% of real newsletters — subject lines like
    "Kann das fliegen?" or "\U0001f7e3 Milliardenrechnung von AWS" don't
    contain literal words like "startup" or "funding".

    newsletter_search_terms (config/sources.yaml) is kept as an OPTIONAL
    narrowing filter, only applied when the list is non-empty. Re-read fresh
    on every call (Phase S: dynamic sources).
    """
    terms = get_newsletter_search_terms()
    date_clause = f"newer_than:{days}d"
    if terms:
        subject_clause = " OR ".join(terms)
        return f"subject:({subject_clause}) {date_clause}"
    return date_clause


class NewsletterIngestor:
    """
    Connects to Gmail, fetches recent VC/startup newsletters, and routes each
    email body through the standard pipeline:

        chunk → candidate_filter → extract_startups (7B) → upsert_startup()

    This means newsletter-sourced startups go through the same fingerprint
    dedup, deterministic scoring, and Qdrant sync as web + RSS sources.
    A startup seen in a newsletter and on an accelerator site resolves to
    one deduplicated row via the same stable UUID.

    Incremental fetch: processed Gmail message IDs are tracked in
    credentials/newsletter_state.json so each scheduler run only handles
    new mail.
    """

    def __init__(self):
        self._service = None

    # ── Authentication ────────────────────────────────────────────────────────

    def _authenticate(self):
        """OAuth2 authentication with Gmail API."""
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as refresh_err:
                    logger.error(
                        "[Gmail] Token refresh failed — deleting expired token. "
                        "Re-run the process to open a new browser login flow. "
                        f"Details: {refresh_err}"
                    )
                    if os.path.exists(TOKEN_PATH):
                        os.remove(TOKEN_PATH)
                    raise RuntimeError(
                        "Gmail OAuth token expired and could not be refreshed. "
                        "Delete credentials/token.json and restart to re-authenticate."
                    ) from refresh_err
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    settings.gmail_credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            os.makedirs(os.path.join(_PROJECT_ROOT, "credentials"), exist_ok=True)
            with open(TOKEN_PATH, "w") as token_file:
                token_file.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("[Gmail] Authenticated successfully")

    # ── Incremental-fetch state ───────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load processed-message-ID set from disk."""
        if os.path.exists(_STATE_PATH):
            try:
                with open(_STATE_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"processed_ids": []}

    def _save_state(self, state: dict) -> None:
        """Persist processed-message-ID set to disk."""
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f)

    # ── Main entry point ──────────────────────────────────────────────────────

    def run_ingestion(self, max_messages: int = 50, days: int = 14) -> int:
        """
        Fetch and process Gmail newsletters. Returns total startups stored.
        Already-processed messages are skipped via the state file.

        days: the search window (see _build_search_query). Default 14 for
          the routine scheduled top-up. Pass a much larger value (e.g. 3650)
          for a one-time backfill sweep of older mail the rolling window
          never reaches — safe to re-run any time since already-processed
          messages are always skipped regardless of window size.

        max_messages: caps how many NEW messages get PROCESSED this run —
          it does NOT cap how many are listed (see _list_all_message_ids;
          listing paginates through the full match set). This matters for
          a backfill: without pagination, a >50-message window would
          silently see only its 50 most-recent matches and permanently miss
          the rest, exactly the bug that caused this fix (confirmed live:
          a 61-message mailbox has more matches than the old maxResults=50
          listing cap allowed, so anything past page one was invisible on
          every single run, forever).
        """
        if not self._service:
            self._authenticate()

        state = self._load_state()
        processed_ids: set = set(state.get("processed_ids", []))

        all_ids = self._list_all_message_ids(_build_search_query(days))
        logger.info(f"[Gmail] {len(all_ids)} messages match the {days}-day window")

        new_processed: list = []
        total_startups = 0

        for msg_id in all_ids:
            if msg_id in processed_ids:
                logger.debug(f"[Gmail] Skipping already-processed message {msg_id}")
                continue
            if len(new_processed) >= max_messages:
                logger.info(
                    f"[Gmail] Reached max_messages={max_messages} for this run — "
                    f"{len(all_ids) - len(processed_ids) - len(new_processed)} more "
                    "new message(s) remain for the next run"
                )
                break

            count = self._process_message(msg_id)
            total_startups += count
            new_processed.append(msg_id)

        if new_processed:
            # Retain only the last 2000 IDs so the state file stays small but
            # still comfortably covers a full-mailbox backfill (500 was sized
            # for the old 14-day-only window; a backfill can legitimately
            # process far more than 500 messages in its lifetime).
            retained_ids = list(processed_ids) + new_processed
            state["processed_ids"] = retained_ids[-2000:]
            self._save_state(state)

        logger.info(
            f"[Gmail] Done — {len(new_processed)} new emails processed, "
            f"{len(all_ids) - len(new_processed)} already seen or beyond this run's cap, "
            f"{total_startups} startups stored"
        )
        return total_startups

    def _list_all_message_ids(self, query: str, *, page_size: int = 100, hard_cap: int = 5000) -> list:
        """
        List every message ID matching `query`, following Gmail's
        nextPageToken across as many pages as it takes — the previous
        version passed maxResults straight through with no pagination, so
        any window with more than maxResults (default 50) matches silently
        lost everything past the first page. hard_cap is just a sanity
        backstop against a runaway loop; a real mailbox won't get close.
        """
        ids: list = []
        page_token = None
        while True:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=page_size, pageToken=page_token)
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(ids) >= hard_cap:
                break
        return ids

    # ── Private helpers ───────────────────────────────────────────────────────

    def _process_message(self, message_id: str) -> int:
        """Fetch and process one Gmail message. Returns startups stored."""
        try:
            message = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

            headers = {
                h["name"]: h["value"]
                for h in message.get("payload", {}).get("headers", [])
            }
            sender  = headers.get("From", "")
            subject = headers.get("Subject", "")
            date_str = headers.get("Date", "")

            trusted_senders = get_newsletter_senders()
            if trusted_senders and not self._is_trusted_sender(sender, trusted_senders):
                logger.debug(f"[Gmail] Skipping untrusted sender: {sender!r}")
                return 0

            text = self._extract_text(message)
            if not text or len(text) < 100:
                return 0

            published_date = self._parse_email_date(date_str)
            source_url = f"gmail://{message_id}"

            provenance = {
                "source_name": self._extract_sender_name(sender),
                "sender": sender,
                "subject": subject,
            }
            count = self._extract_and_store(text, published_date, source_url, provenance, message_id)
            self._save_email_record(message_id, subject, sender, text, count)
            return count

        except Exception as exc:
            logger.error(f"[Gmail] Failed to process message {message_id}: {exc}")
            return 0

    def _is_trusted_sender(self, sender: str, trusted_senders: List[str]) -> bool:
        """Return True if sender matches any entry from config/sources.yaml's newsletter_senders."""
        sender_lower = sender.lower()
        return any(t.lower() in sender_lower for t in trusted_senders)

    def _extract_sender_name(self, sender: str) -> str:
        """
        Extract the display name from a From header for a human-readable
        source_name, e.g. '"KIT-Gründerschmiede" <x@kit.edu>' -> 'KIT-Gründerschmiede'.
        Falls back to the raw header if there's no quoted display name.
        """
        import re
        match = re.match(r'^"?([^"<]+?)"?\s*<', sender)
        return match.group(1).strip() if match else sender

    def _extract_text(self, message: dict) -> str:
        """Extract clean plain text from a Gmail message payload."""

        def _decode_part(payload: dict) -> str:
            mime = payload.get("mimeType", "")
            body_data = payload.get("body", {}).get("data", "")

            if mime == "text/plain" and body_data:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")

            if mime == "text/html" and body_data:
                html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                return soup.get_text(separator="\n", strip=True)

            for part in payload.get("parts", []):
                text = _decode_part(part)
                if text:
                    return text
            return ""

        return _decode_part(message.get("payload", {}))

    def _extract_and_store(
        self,
        text: str,
        published_date: Optional[str],
        source_url: str,
        provenance: dict,
        message_id: str,
    ) -> int:
        """
        Route email body through the standard pipeline:
          chunk → candidate_filter → extract_startups → upsert_startup

        Returns total startups stored (new inserts + dedup merges).
        """
        from ingestion.chunker import split_blurbs
        from ingestion.candidate_filter import is_relevant
        from reasoning.qwen_client import qwen_client
        from processing.storage import upsert_startup

        # Phase H-1: newsletters are a sequence of short, independent
        # company blurbs — split_blurbs keeps one company per chunk (no
        # overlap), which is what stops cross-attribution between
        # neighboring companies in the same digest. See chunker.py docstring.
        chunks = split_blurbs(text)
        relevant = [c for c in chunks if is_relevant(c)]

        logger.info(
            f"[Gmail] {message_id}: {len(chunks)} chunk(s), {len(relevant)} relevant"
        )

        inserted = 0
        staged   = 0

        for chunk in relevant:
            try:
                startups = qwen_client.extract_startups(chunk)
                for startup in startups:
                    if not startup.get("name"):
                        continue
                    # Back-fill published_date from email header when LLM left it blank
                    if not startup.get("published_date") and published_date:
                        startup["published_date"] = published_date

                    record_id, status = upsert_startup(
                        startup,
                        source="newsletter",
                        source_url=source_url,
                        published_date=published_date,
                        provenance=provenance,
                    )
                    if status == "new_master":
                        inserted += 1
                    elif status in ("staged_update", "staged_duplicate", "staged_anomaly"):
                        staged += 1

            except Exception as exc:
                logger.warning(
                    f"[Gmail] Chunk extraction failed for {message_id}: {exc}"
                )

        logger.info(
            f"[Gmail] {message_id}: {inserted} new master(s), {staged} staged for review"
        )
        return inserted + staged

    def _parse_email_date(self, date_str: str) -> Optional[str]:
        """Parse RFC 2822 email Date header into an ISO 8601 date string."""
        from email.utils import parsedate_to_datetime
        if not date_str:
            return None
        try:
            return parsedate_to_datetime(date_str).date().isoformat()
        except Exception:
            return None

    def _save_email_record(
        self,
        message_id: str,
        subject: str,
        sender: str,
        text: str,
        startup_count: int,
    ) -> None:
        """Write a NewsletterEntry row for audit/traceability. Startups are in the main table."""
        try:
            from database.connection import SessionLocal
            from database.models import NewsletterEntry

            db = SessionLocal()
            try:
                entry = NewsletterEntry(
                    subject=subject[:500],
                    sender=sender[:255],
                    received_at=datetime.utcnow(),
                    raw_text=text[:10000],
                    extracted_startups=[],   # startups now persisted via upsert_startup
                    startup_count=startup_count,
                    processed=True,
                )
                db.add(entry)
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error(f"[Gmail] Failed to save email record for {message_id}: {exc}")


newsletter_ingestor = NewsletterIngestor()
