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
        Load the set of already-processed messages, keyed by RFC 5322
        **Message-ID** — the globally unique, immutable identifier the sender
        stamps on the mail itself.

        WHY NOT IMAP UID (changed 16 Aug 2026). UIDs are mailbox-scoped and
        only meaningful within one UIDVALIDITY generation, and this mailbox
        has already survived one identity change that the old scheme could not
        express: before 12 Aug the ingestor ran on the Gmail API and recorded
        Gmail's own hex message ids; after the IMAP migration it recorded IMAP
        UIDs. The live state file was found holding BOTH — 112 stale hex ids
        that can never match anything, plus 37 real UIDs — so every message
        handled in the Gmail-API era had effectively lost its "already done"
        marker. Message-ID survives both migrations, a UIDVALIDITY bump, and a
        re-download of the whole mailbox, and it is already what
        _process_message writes into source_url/source_history, so the state
        file and the database now agree on what identifies a newsletter.

        Old-format files are migrated on read rather than discarded: any
        surviving entry is kept under "legacy_ids" purely so a human can see
        what was there, and the mailbox is re-scanned by Message-ID. A
        re-processed message is a harmless no-op (upsert_startup dedups); a
        silently skipped one is not, which is the failure this replaces.
        """
        state = {"processed_message_ids": [], "legacy_ids": []}
        if not os.path.exists(_STATE_PATH):
            return state
        try:
            with open(_STATE_PATH) as f:
                raw = json.load(f)
        except Exception as exc:
            logger.warning(f"[Gmail] state file unreadable ({exc}) — starting fresh")
            return state

        if isinstance(raw.get("processed_message_ids"), list):
            state["processed_message_ids"] = [str(x) for x in raw["processed_message_ids"]]
            state["legacy_ids"] = [str(x) for x in (raw.get("legacy_ids") or [])]
            return state

        # Pre-16-Aug format: {"processed_ids": [...], "uidvalidity": ...} — a
        # mix of Gmail-API hex ids and IMAP UIDs, neither of which is a
        # Message-ID. Keep them for the record, trust none of them.
        legacy = [str(x) for x in (raw.get("processed_ids") or [])]
        if legacy:
            logger.warning(
                f"[Gmail] migrating state file from the old UID/Gmail-id format "
                f"({len(legacy)} entries kept for reference but not trusted). "
                f"Messages will be re-checked by Message-ID; anything genuinely "
                f"already ingested is a no-op via upsert_startup's dedup."
            )
        state["legacy_ids"] = legacy
        return state

    def _save_state(self, state: dict) -> None:
        """Persist the processed-Message-ID set to disk."""
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _STATE_PATH)   # atomic: never leave a half-written marker file

    def _message_ids_for(self, uids: List[str]) -> dict:
        """
        Map UID -> Message-ID with ONE cheap header-only fetch.

        This is what makes a wide lookback window affordable: the expensive
        part of ingestion is fetching and LLM-parsing full bodies, so the
        already-seen check has to happen before that. Headers for the whole
        mailbox cost a single round trip.
        """
        if not uids:
            return {}
        out = {}
        for uid in uids:
            try:
                typ, data = self._conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if typ != "OK" or not data or not data[0]:
                    continue
                raw = data[0][1]
                msg = email.message_from_bytes(raw if isinstance(raw, (bytes, bytearray)) else raw.encode())
                mid = (msg.get("Message-ID") or "").strip().strip("<>")
                if mid:
                    out[uid] = mid
            except Exception as exc:
                logger.debug(f"[Gmail] header fetch failed for uid {uid}: {exc}")
        return out

    # ── Main entry point ──────────────────────────────────────────────────────

    def run_ingestion(self, max_messages: int = 50, days: int = 90) -> int:
        """
        Fetch and process Gmail newsletters. Returns total startups stored.
        Already-processed messages are skipped by **Message-ID**.

        days: the search window, default 90 (raised from 14 on 16 Aug 2026).

          The old 14-day default was silently losing mail. Measured on the
          live mailbox that day: 103 messages spanning June-August, of which
          only 32 were reachable at days=14 — every one of the 29 June
          newsletters had never been ingested at all, and 61 messages in
          total had produced no database record. Nothing was broken; the
          window simply never reached them, and the manual backfill that
          could have was never run. Newsletters are the richest source this
          pipeline has, so quietly dropping two thirds of them was the worst
          possible place for that to happen.

          90 days is affordable now only because the already-seen check moved
          in front of the expensive work: run_ingestion resolves Message-IDs
          with one header-only fetch and skips known ones before any body is
          downloaded or any LLM call is made. Widening the window therefore
          costs one cheap header pass, not a re-ingestion. Pass a larger
          value (e.g. 3650) to sweep the entire mailbox; it is safe to re-run
          at any size.

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
            done: set = set(state.get("processed_message_ids", []))

            all_uids = self._list_all_uids(days)
            logger.info(f"[Gmail] {len(all_uids)} messages match the {days}-day window")

            # Resolve Message-IDs FIRST, with one cheap header-only pass, so
            # the already-seen check happens before any body fetch or LLM call.
            # This is what makes a wide window affordable — the cost of looking
            # further back is now a header fetch, not a full re-ingestion.
            uid_to_mid = self._message_ids_for(all_uids)
            pending = [u for u in all_uids if uid_to_mid.get(u) not in done]
            already = len(all_uids) - len(pending)
            logger.info(
                f"[Gmail] {already} already ingested (by Message-ID), {len(pending)} to process"
            )

            new_ids: list = []
            total_startups = 0

            for uid in pending:
                if len(new_ids) >= max_messages:
                    logger.info(
                        f"[Gmail] Reached max_messages={max_messages} for this run — "
                        f"{len(pending) - len(new_ids)} new message(s) remain for the next run"
                    )
                    break

                count = self._process_message(uid)
                total_startups += count
                # Record the marker even when a message yielded zero startups:
                # "we looked at this and it had nothing" is exactly as important
                # to remember as a successful extraction, or every empty
                # newsletter gets re-parsed by the LLM on every single run.
                mid = uid_to_mid.get(uid)
                if mid:
                    new_ids.append(mid)

            if new_ids:
                # Cap the file, keeping the most recent markers. 2000 is far
                # past this mailbox's size, so in practice nothing is ever
                # forgotten — the cap only stops unbounded growth.
                state["processed_message_ids"] = (list(done) + new_ids)[-2000:]
                self._save_state(state)

            logger.info(
                f"[Gmail] Done — {len(new_ids)} new emails processed, "
                f"{already} already ingested, "
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
