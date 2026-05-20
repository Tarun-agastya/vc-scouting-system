import os
import base64
import uuid
import logging
from typing import List
from datetime import datetime
from bs4 import BeautifulSoup
from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Absolute path — safe regardless of which directory the process starts from
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.path.join(_PROJECT_ROOT, "credentials", "token.json")

NEWSLETTER_SEARCH_QUERY = (
    "subject:(startup OR venture OR newsletter OR funding OR accelerator OR incubator) "
    "newer_than:14d"
)


class NewsletterIngestor:
    """
    Connects to Gmail, fetches recent VC/startup newsletters,
    extracts startup entities using Qwen, and stores them in Qdrant.
    """

    def __init__(self):
        self._service = None

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
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    settings.gmail_credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            os.makedirs("./credentials", exist_ok=True)
            with open(TOKEN_PATH, "w") as token_file:
                token_file.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("[Gmail] Authenticated successfully")

    def run_ingestion(self, max_messages: int = 50) -> int:
        """Run full newsletter ingestion pipeline. Returns number of startups found."""
        if not self._service:
            self._authenticate()

        result = (
            self._service.users()
            .messages()
            .list(userId="me", q=NEWSLETTER_SEARCH_QUERY, maxResults=max_messages)
            .execute()
        )

        messages = result.get("messages", [])
        logger.info(f"[Gmail] Found {len(messages)} matching emails")

        total_startups = 0
        for msg in messages:
            count = self._process_message(msg["id"])
            total_startups += count

        logger.info(f"[Gmail] Total startups extracted: {total_startups}")
        return total_startups

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _process_message(self, message_id: str) -> int:
        """Process one Gmail message. Returns number of startups extracted."""
        try:
            message = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

            text = self._extract_text(message)
            if not text or len(text) < 100:
                return 0

            startups = self._extract_startups(text)

            # Persist the email record
            self._save_email_record(message, text, startups)
            return len(startups)

        except Exception as exc:
            logger.error(f"[Gmail] Failed to process message {message_id}: {exc}")
            return 0

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

    def _extract_startups(self, text: str) -> List[dict]:
        """Use Qwen to extract startup mentions from email text."""
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import NEWSLETTER_EXTRACTION_PROMPT
        from embeddings.embedder import embedder
        from vector_db.qdrant_store import qdrant_store

        try:
            prompt = NEWSLETTER_EXTRACTION_PROMPT.format(text=text[:3500])
            response = qwen_client.generate(
                prompt,
                system="Return ONLY valid JSON array. No explanation.",
                temperature=0.0,
            )

            startups: List[dict] = qwen_client.parse_json_array(response)
            stored = []

            for startup in startups:
                if not startup.get("name"):
                    continue
                startup_id = str(uuid.uuid4())
                embed_text = embedder.build_startup_text(startup)
                vector = embedder.embed(embed_text)
                payload = {**startup, "source": "newsletter", "id": startup_id}
                qdrant_store.upsert_startup(startup_id, vector, payload)
                stored.append(startup)

            return stored

        except Exception as exc:
            logger.debug(f"[Gmail] Extraction failed: {exc}")
            return []

    def _save_email_record(self, message: dict, text: str, startups: List[dict]):
        """Persist newsletter entry to PostgreSQL."""
        try:
            from database.connection import SessionLocal
            from database.models import NewsletterEntry

            headers = {
                h["name"]: h["value"]
                for h in message.get("payload", {}).get("headers", [])
            }

            db = SessionLocal()
            entry = NewsletterEntry(
                subject=headers.get("Subject", "")[:500],
                sender=headers.get("From", "")[:255],
                received_at=datetime.utcnow(),
                raw_text=text[:10000],
                extracted_startups=startups,
                startup_count=len(startups),
                processed=True,
            )
            db.add(entry)
            db.commit()
            db.close()
        except Exception as exc:
            logger.error(f"[Gmail] Failed to save email record: {exc}")


newsletter_ingestor = NewsletterIngestor()
