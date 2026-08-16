"""
One-time migration of credentials/newsletter_state.json to Message-ID keying.

WHY. Until 16 Aug 2026 the "already processed" marker was an IMAP UID — and
before the 12 Aug Gmail-API -> IMAP migration it was Gmail's own hex message
id. The live state file was found holding BOTH (112 hex + 37 UIDs), so every
message handled in the Gmail-API era had effectively lost its marker, and the
two formats could never be reconciled with each other.

ingestion/newsletter_ingestor.py now keys on the RFC 5322 Message-ID, which is
stable across both migrations, across a UIDVALIDITY bump, and across a full
re-download of the mailbox. It also already IS what _process_message writes
into source_url / source_history, so the state file and the database finally
agree on what identifies a newsletter.

WHAT THIS DOES. Rebuilds the marker set from the two sources that can actually
be trusted:

  1. The DATABASE — every `gmail://<message-id>` in Startup.source_url and
     source_history. If a newsletter produced a record, it was definitively
     ingested. Only the RFC-form ids are usable; the pre-12-Aug hex ones name
     a Gmail API id that no longer resolves to anything, so they are counted
     and reported but cannot be mapped.

  2. The MAILBOX — the UIDs still listed in the old state file are resolved to
     their Message-IDs over IMAP, recovering the markers for messages
     processed since 12 Aug.

Anything that cannot be recovered is simply left unmarked and will be
re-ingested on the next run. That is the safe direction: re-processing is a
no-op thanks to upsert_startup's dedup, whereas a wrongly-set marker would
hide a newsletter forever — which is the bug this whole change exists to fix.

Dry run by default.

Usage:
    python3 scripts/migrate_newsletter_state.py
    python3 scripts/migrate_newsletter_state.py --apply
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.newsletter_ingestor import _STATE_PATH  # noqa: E402

_HEX_GMAIL_ID = re.compile(r"^[0-9a-f]{16}$")


def _db_message_ids():
    """Every gmail:// id the database has on record, split by format."""
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    try:
        ids = set()
        for (u,) in db.query(Startup.source_url).filter(Startup.source_url.like("gmail://%")).all():
            ids.add(u.replace("gmail://", ""))
        for s in db.query(Startup).filter(Startup.source_history.isnot(None)).all():
            for h in (s.source_history or []):
                u = (h.get("url") or "")
                if u.startswith("gmail://"):
                    ids.add(u.replace("gmail://", ""))
    finally:
        db.close()

    usable = {i for i in ids if "@" in i}                 # real RFC Message-IDs
    unmappable = {i for i in ids if _HEX_GMAIL_ID.match(i)}  # pre-12-Aug Gmail API ids
    return usable, unmappable


def _mailbox_message_ids(old_uids):
    """Resolve the old state file's surviving IMAP UIDs to Message-IDs."""
    if not old_uids:
        return set(), 0
    from ingestion.newsletter_ingestor import newsletter_ingestor

    ing = newsletter_ingestor
    ing._authenticate()
    try:
        mapping = ing._message_ids_for([str(u).encode() for u in old_uids])
        return set(mapping.values()), len(old_uids)
    finally:
        try:
            ing._conn.logout()
        except Exception:
            pass
        ing._conn = None


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate the newsletter state file to Message-ID keying")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    old = {}
    if os.path.exists(_STATE_PATH):
        with open(_STATE_PATH) as f:
            old = json.load(f)

    if isinstance(old.get("processed_message_ids"), list):
        print("State file is already in Message-ID format — nothing to migrate.")
        print(f"  markers: {len(old['processed_message_ids'])}")
        return 0

    legacy = [str(x) for x in (old.get("processed_ids") or [])]
    old_uids = [x for x in legacy if x.isdigit()]
    old_hex = [x for x in legacy if _HEX_GMAIL_ID.match(x)]
    print(f"Old state file: {len(legacy)} entries — {len(old_hex)} Gmail-API hex ids, {len(old_uids)} IMAP UIDs")

    from_db, unmappable = _db_message_ids()
    print(f"From database : {len(from_db)} usable Message-IDs "
          f"({len(unmappable)} pre-12-Aug hex ids cannot be mapped)")

    from_mailbox, tried = _mailbox_message_ids(old_uids)
    print(f"From mailbox  : {len(from_mailbox)} Message-IDs recovered from {tried} old UIDs")

    recovered = sorted(from_db | from_mailbox)
    print(f"\nTotal markers after migration: {len(recovered)}")
    print(f"Left unmarked (will be re-checked, re-ingestion is a harmless no-op): "
          f"{len(unmappable)} hex-era message(s)")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    new_state = {"processed_message_ids": recovered, "legacy_ids": legacy}
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f, indent=2)
    os.replace(tmp, _STATE_PATH)
    print(f"\n✓ Written: {_STATE_PATH} ({len(recovered)} markers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
