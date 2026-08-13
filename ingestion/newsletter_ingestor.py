import email
import imaplib
import json
import os
import logging
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import List, Optional
from bs4 import BeautifulSoup
from config.source_loader import get_newsletter_search_terms, get_newsletter_senders

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_PATH  = os.path.join(_PROJECT_ROOT, "credentials", "newsletter_state.json")


def _decode_header_value(raw: Optional[str]) -> str:
    """RFC 2047 header decoding — raw IMAP fetch keeps Subject/From as
    MIME encoded-words (e.g. '=?UTF-8?B?...?=') for anything non-ASCII,
    unlike the old Gmail API which returned already-decoded strings. Subject
    lines like "🟣 Milliardenrechnung von AWS" (see run_ingestion's
    docstring) need this or candidate_filter/extraction never see them."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _imap_since_date(days: int) -> str:
    """'SINCE "dd-Mon-YYYY"' criterion, IMAP's own date syntax (day
    granularity, not Gmail's relative newer_than:Nd — close enough for a
    rolling window; a message landing exactly on the boundary is picked up
    by whichever run runs next, and re-processing is a no-op either way
    since already-processed UIDs are always skipped)."""
    return (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")


class NewsletterIngestor:
    """
    Connects to Gmail, fetches recent VC/startup newsletters, and routes each
    email body through the standard pipeline:

        chunk → candidate_filter → extract_startups (7B) → upsert_startup()

    This means newsletter-sourced startups go through the same fingerprint
    dedup, deterministic scoring, and Qdrant sync as web + RSS sources.
    A startup seen in a newsletter and on an accelerator site resolves to
    one deduplicated row via the same stable UUID.

    Incremental fetch: processed IMAP UIDs are tracked in
    credentials/newsletter_state.json so each scheduler run only handles
    new mail. Message-ID (the email header, globally stable) is used for the
    source_url/provenance shown downstream, since raw IMAP UIDs are only
    guaranteed stable within one UIDVALIDITY session — see _load_state.
    """

    def __init__(self):
        self._conn = None

    # ── Authentication ────────────────────────────────────────────────────────

    def _authenticate(self):
        """
        IMAP login via App Password — shared with press_monitor/emailer.py's
        SMTP login via ingestion/gmail_auth.py (Phase PM, 12 Aug 2026) so
        both features read the same GMAIL_ADDRESS/GMAIL_APP_PASSWORD rather
        than each carrying its own credential path.
        """
        from ingestion.gmail_auth import get_imap_connection
        self._conn = get_imap_connection()
        self._conn.select("INBOX")
        logger.info("[Gmail] Authenticated successfully (IMAP)")

    # ── Incremental-fetch state ───────────────────────────────────────────────

    def _load_state(self) -> dict:
        """
        Load processed-UID set from disk, keyed to the mailbox's current
        UIDVALIDITY. IMAP UIDs are only meaningful within one UIDVALIDITY
        generation — if Gmail ever rebuilds the mailbox and bumps it, old
        UIDs could silently refer to different messages. Detected here by
        comparing against the connected mailbox's live UIDVALIDITY (set by
        _authenticate's SELECT) and the stale state is discarded rather than
        trusted — reprocessing a message once is a harmless no-op thanks to
        upsert_startup's dedup, silently mismatching UIDs is not.
        """
        current_uidvalidity = None
        if self._conn is not None:
            status, data = self._conn.response("UIDVALIDITY")
            if data and data[0]:
                current_uidvalidity = data[0].decode() if isinstance(data[0], bytes) else str(data[0])

        if os.path.exists(_STATE_PATH):
            try:
                with open(_STATE_PATH) as f:
                    state = json.load(f)
                if current_uidvalidity and state.get("uidvalidity") not in (None, current_uidvalidity):
                    logger.warning(
                        f"[Gmail] UIDVALIDITY changed ({state.get('uidvalidity')} -> "
                        f"{current_uidvalidity}) — old processed-UID list is no longer "
                        "trustworthy, starting fresh (re-processed messages are a no-op)"
                    )
                    return {"processed_ids": [], "uidvalidity": current_uidvalidity}
                state.setdefault("uidvalidity", current_uidvalidity)
                return state
            except Exception:
                pass
        return {"processed_ids": [], "uidvalidity": current_uidvalidity}

    def _save_state(self, state: dict) -> None:
        """Persist processed-UID set to disk."""
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f)

    # ── Main entry point ──────────────────────────────────────────────────────

    def run_ingestion(self, max_messages: int = 50, days: int = 14) -> int:
        """
        Fetch and process Gmail newsletters. Returns total startups stored.
        Already-processed messages are skipped via the state file.

        days: the search window. Default 14 for the routine scheduled
          top-up. Pass a much larger value (e.g. 3650) for a one-time
          backfill sweep of older mail the rolling window never reaches —
          safe to re-run any time since already-processed messages are
          always skipped regardless of window size.

        max_messages: caps how many NEW messages get PROCESSED this run —
          it does NOT cap how many are listed. IMAP SEARCH returns every
          matching UID in one round-trip (no Gmail-API-style page cap to
          worry about), so unlike the old version this needs no pagination
          loop to avoid silently losing anything past a first page.

        newsletter_search_terms (config/sources.yaml), when non-empty, is
        applied as a client-side subject-substring filter AFTER the date
        search rather than folded into the IMAP SEARCH itself — IMAP's OR
        syntax is a binary tree that gets unwieldy past a couple of terms,
        and this mailbox is small enough that fetching-then-filtering costs
        nothing noticeable. Empty (the routine default) means everything in
        the window is fetched and relevance is filtered by content
        downstream (candidate_filter.is_relevant, per chunk) — not by
        guessing what words a newsletter's subject line contains, which
        used to silently drop ~85% of real newsletters (subject lines like
        "Kann das fliegen?" don't contain literal words like "startup").
        """
        if not self._conn:
            self._authenticate()

        try:
            state = self._load_state()
            processed_ids: set = set(state.get("processed_ids", []))

            all_uids = self._list_all_uids(days)
            logger.info(f"[Gmail] {len(all_uids)} messages match the {days}-day window")

            new_processed: list = []
            total_startups = 0

            for uid in all_uids:
                if uid in processed_ids:
                    logger.debug(f"[Gmail] Skipping already-processed message {uid}")
                    continue
                if len(new_processed) >= max_messages:
                    logger.info(
                        f"[Gmail] Reached max_messages={max_messages} for this run — "
                        f"{len(all_uids) - len(processed_ids) - len(new_processed)} more "
                        "new message(s) remain for the next run"
                    )
                    break

                count = self._process_message(uid)
                total_startups += count
                new_processed.append(uid)

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
                f"{len(all_uids) - len(new_processed)} already seen or beyond this run's cap, "
                f"{total_startups} startups stored"
            )
            return total_startups
        finally:
            # Always drop the connection, success or failure — this is a
            # module-level singleton reused across scheduled runs (daily
            # Gmail top-up). A logout()/search() raising mid-run used to
            # leave self._conn set to a dead connection object forever
            # (truthy, so `if not self._conn` above would never
            # re-authenticate), silently breaking every future run until
            # the API process was restarted. Swallow a failing logout()
            # itself (the connection may already be gone) — the reset to
            # None is what actually matters.
            try:
                if self._conn:
                    self._conn.logout()
            except Exception as exc:
                logger.debug(f"[Gmail] logout() failed (connection likely already dead): {exc}")
            finally:
                self._conn = None

    def _list_all_uids(self, days: int) -> List[str]:
        """List every message UID in the last `days`, newest search
        semantics matching IMAP's own SINCE criterion (see
        _imap_since_date). Subject-term narrowing, when configured, is
        applied client-side after fetch — see run_ingestion's docstring."""
        typ, data = self._conn.search(None, f'(SINCE "{_imap_since_date(days)}")')
        if typ != "OK" or not data or not data[0]:
            return []
        return [uid.decode() if isinstance(uid, bytes) else uid for uid in data[0].split()]

    # ── Private helpers ───────────────────────────────────────────────────────

    def _process_message(self, uid: str) -> int:
        """Fetch and process one Gmail message by IMAP UID. Returns startups stored."""
        message_id = uid  # overwritten below once the real Message-ID header is known
        try:
            typ, data = self._conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                logger.warning(f"[Gmail] UID fetch returned nothing for {uid}")
                return 0
            raw = data[0][1]
            message = email.message_from_bytes(raw)

            sender   = _decode_header_value(message.get("From", ""))
            subject  = _decode_header_value(message.get("Subject", ""))
            date_str = message.get("Date", "")
            # Message-ID is the globally-stable identifier for source_url/
            # provenance — the IMAP UID (this method's `uid` param) only
            # drives the local skip-check/state file, see _load_state.
            message_id = (message.get("Message-ID") or f"uid-{uid}").strip("<>")

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
            logger.error(f"[Gmail] Failed to process message uid={uid}: {exc}")
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

    def _extract_text(self, message: "email.message.Message") -> str:
        """
        Extract clean plain text from a parsed RFC822 message. Same
        preference order as the old Gmail-API version: prefer text/plain,
        fall back to text/html stripped via BeautifulSoup, recursing into
        multipart parts in order and returning the first non-empty result.
        """

        def _decode_part(part: "email.message.Message") -> str:
            if part.is_multipart():
                for sub in part.get_payload():
                    text = _decode_part(sub)
                    if text:
                        return text
                return ""

            payload = part.get_payload(decode=True)
            if not payload:
                return ""
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="ignore")
            except (LookupError, UnicodeDecodeError):
                decoded = payload.decode("utf-8", errors="ignore")

            ctype = part.get_content_type()
            if ctype == "text/plain":
                return decoded
            if ctype == "text/html":
                soup = BeautifulSoup(decoded, "html.parser")
                return soup.get_text(separator="\n", strip=True)
            return ""

        return _decode_part(message)

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
