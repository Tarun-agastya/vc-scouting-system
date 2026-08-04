# GreenTech Hub Press Monitor

Daily scan of the Allgäuer/Memminger Zeitung e-paper for mentions of
GreenTech Hub, its portfolio startups, and its 15 partner companies. Sends
a short German-language summary + a screenshot of the matched page to
`press_monitor_recipients`, only when something is actually found.

## How it works

1. **Login** (`epaper_client.py`) — Playwright logs in as a real subscriber
   (Corinna's account) at `webepaper.allgaeuer-zeitung.de`, finds the
   current day's Memminger Zeitung edition on the dashboard, and downloads
   it via the site's own "Gesamtausgabe (PDF) laden" button — the same
   action a subscriber takes manually, nothing more.
2. **Scan** (`scanner.py`) — the downloaded PDF carries a full embedded
   text layer (confirmed live 4 Aug 2026 — no OCR needed). Every page's
   text is checked against `keywords.yaml`; a matched page gets a
   screenshot rendered (PyMuPDF) and a text excerpt captured.
3. **Summarize** (`summarizer.py`) — the local Ollama reasoning model
   (same one used elsewhere in this project, fully local/private) writes a
   2–3 sentence German summary per match, and is explicitly asked to flag
   when a match is likely a false positive (e.g. "Reisacher" the company
   vs. a person named Reisacher — confirmed live, it correctly told them apart).
4. **Email** (`emailer.py`) — one digest email per day with a match, sent
   via the Gmail API using the same OAuth token as the newsletter reader
   (`ingestion/gmail_auth.py` — one consent grants both `gmail.readonly`
   and `gmail.send`), sent only if at least one match was found.
5. The downloaded PDF and rendered page screenshots are deleted after the
   email is sent (or if nothing matched) — no local copy of the full
   copyrighted edition is retained.

## Setup — status

All in `.env` (already gitignored, never committed):

```
epaper_email=corinna.tappe@gt-hub.de       # already set
epaper_password=...                         # already set
press_monitor_recipients=corinna.tappe@gt-hub.de,stefan.lenz@gt-hub.de  # already set
```

Sending is fully set up: `credentials/token.json` holds an OAuth token for
`greentechhubx@gmail.com` (note: double "h" — a single-letter typo in this
address is what caused the original SMTP app-password attempts to fail;
nothing was actually wrong with any of the three app passwords generated
along the way) covering both `gmail.readonly` (newsletter reading) and
`gmail.send` (this feature) from one consent grant. A real test digest was
sent successfully to both recipients on 4 Aug 2026.

If `credentials/token.json` is ever deleted or its refresh token stops
working, re-running anything that calls `ingestion.gmail_auth.get_gmail_service()`
(the scheduled job, `run_daily.py`, or the newsletter reader) will reopen
the browser consent flow — needs a human present to click "Allow" once.

## Running it

- **Scheduled**: runs automatically every day at 08:00 as part of the main
  API service (`api/main.py`'s background scheduler, job id
  `press_monitor`) — well after the e-paper typically publishes, well
  before the 13:00 Gmail newsletter top-up so the two never collide.
  Silently no-ops if `epaper_email`/`epaper_password` aren't set.
- **Manual / one-off**:
  ```
  python -m press_monitor.run_daily                    # today's edition
  python -m press_monitor.run_daily --date 2026-08-03   # a specific date
  ```

## Editing the watch list

Edit `keywords.yaml` directly — no code change, no redeploy needed (the
scheduled job reads it fresh each run). Matching is case-insensitive
literal substring, deliberately **not** filtered for false positives (a
short/common name like "Reisacher" will sometimes match an unrelated
person) — every hit goes to a human via the digest with its own summary
and screenshot for a quick glance, same "evidence not guessing" stance as
the rest of the GreenTech Hub scouting system this lives alongside.

## Known limitations

- **Scoped to the Memminger Zeitung edition only.** The e-paper platform
  also serves several other regional Allgäuer Zeitung editions (Kempten,
  Kaufbeuren, Füssen, Marktoberdorf, etc.) — not scanned. Extending to
  those would mean confirming the subscription covers them and adding
  another `download_todays_edition()` call per edition.
- **Terms of service.** Automating login to a paid subscription e-paper is
  a real consideration flagged before building this — proceeding was an
  explicit decision (4 Aug 2026), not an oversight. Worth periodically
  confirming this remains acceptable under the subscription's terms.
- **One edition per day, no retry.** If the scheduled 08:00 run hits
  before that day's edition is published, it logs `not_published` and
  waits for tomorrow's run rather than retrying later the same day.
