import json
import os
import base64
import logging
from datetime import datetime
from typing import List, Optional
from bs4 import BeautifulSoup
from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH   = os.path.join(_PROJECT_ROOT, "credentials", "token.json")
_STATE_PATH  = os.path.join(_PROJECT_ROOT, "credentials", "newsletter_state.json")

# Gmail search query for incoming VC/startup newsletters
NEWSLETTER_SEARCH_QUERY = (
    "subject:(startup OR venture OR newsletter OR funding OR accelerator OR incubator) "
    "newer_than:14d"
)

# Trusted-sender domain/address allowlist.
# Only emails whose From header contains one of these strings are processed.
# The check is a case-insensitive substring match on the full From header, so
# "sce.de" matches "SCE Newsletter <info@sce.de>".
# Set to an empty list to accept ALL senders matching the search query.
# Empty list = accept all senders. Content relevance is gated downstream
# by candidate_filter.is_relevant() before any LLM call is made.
TRUSTED_NEWSLETTER_SENDERS: List[str] = []


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

    def run_ingestion(self, max_messages: int = 50) -> int:
        """
        Fetch and process Gmail newsletters. Returns total startups stored.
        Already-processed messages are skipped via the state file.
        """
        if not self._service:
            self._authenticate()

        state = self._load_state()
        processed_ids: set = set(state.get("processed_ids", []))

        result = (
            self._service.users()
            .messages()
            .list(userId="me", q=NEWSLETTER_SEARCH_QUERY, maxResults=max_messages)
            .execute()
        )

        messages = result.get("messages", [])
        logger.info(f"[Gmail] Found {len(messages)} matching emails")

        new_processed: list = []
        total_startups = 0

        for msg in messages:
            msg_id = msg["id"]
            if msg_id in processed_ids:
                logger.debug(f"[Gmail] Skipping already-processed message {msg_id}")
                continue

            count = self._process_message(msg_id)
            total_startups += count
            new_processed.append(msg_id)

        if new_processed:
            # Retain only the last 500 IDs so the state file stays small
            all_ids = list(processed_ids) + new_processed
            state["processed_ids"] = all_ids[-500:]
            self._save_state(state)

        logger.info(
            f"[Gmail] Done — {len(new_processed)} new emails processed, "
            f"{len(messages) - len(new_processed)} skipped (already seen), "
            f"{total_startups} startups stored"
        )
        return total_startups

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

            if TRUSTED_NEWSLETTER_SENDERS and not self._is_trusted_sender(sender):
                logger.debug(f"[Gmail] Skipping untrusted sender: {sender!r}")
                return 0

            text = self._extract_text(message)
            if not text or len(text) < 100:
                return 0

            published_date = self._parse_email_date(date_str)
            source_url = f"gmail://{message_id}"

            count = self._extract_and_store(text, published_date, source_url, message_id)
            self._save_email_record(message_id, subject, sender, text, count)
            return count

        except Exception as exc:
            logger.error(f"[Gmail] Failed to process message {message_id}: {exc}")
            return 0

    def _is_trusted_sender(self, sender: str) -> bool:
        """Return True if sender matches any TRUSTED_NEWSLETTER_SENDERS entry."""
        sender_lower = sender.lower()
        return any(t.lower() in sender_lower for t in TRUSTED_NEWSLETTER_SENDERS)

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
        message_id: str,
    ) -> int:
        """
        Route email body through the standard pipeline:
          chunk → candidate_filter → extract_startups → upsert_startup

        Returns total startups stored (new inserts + dedup merges).
        """
        from ingestion.chunker import split as split_chunks
        from ingestion.candidate_filter import is_relevant
        from reasoning.qwen_client import qwen_client
        from processing.storage import upsert_startup

        chunks = split_chunks(text)
        relevant = [c for c in chunks if is_relevant(c)]

        logger.info(
            f"[Gmail] {message_id}: {len(chunks)} chunk(s), {len(relevant)} relevant"
        )

        inserted = 0
        deduped  = 0

        for chunk in relevant:
            try:
                startups = qwen_client.extract_startups(chunk)
                for startup in startups:
                    if not startup.get("name"):
                        continue
                    # Back-fill published_date from email header when LLM left it blank
                    if not startup.get("published_date") and published_date:
                        startup["published_date"] = published_date

                    record_id, is_new = upsert_startup(
                        startup,
                        source="newsletter",
                        source_url=source_url,
                        published_date=published_date,
                    )
                    if record_id and is_new:
                        inserted += 1
                    elif record_id:
                        deduped += 1

            except Exception as exc:
                logger.warning(
                    f"[Gmail] Chunk extraction failed for {message_id}: {exc}"
                )

        logger.info(
            f"[Gmail] {message_id}: {inserted} new, {deduped} merged into existing"
        )
        return inserted + deduped

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
