# GreenTech Hub Press Monitor — how it actually works

This explains the whole automation end to end: what runs, when, with which
libraries, and why each piece was built the way it was. It's a companion to
`README.md` (which is the terse "how to operate it" doc) — this one is the
"understand it" doc.

## The one-paragraph version

Every morning at 08:00, a small independent program logs into the Allgäuer/
Memminger Zeitung's e-paper website as a real subscriber, downloads that
day's full newspaper as a PDF, searches every page for ~30 watched names
(GreenTech Hub, its portfolio startups, its 15 partner companies), and — only
if it finds something — writes a short German summary of each hit, crops a
screenshot of just that article, and emails the whole thing to Corinna,
Stefan, and now Franziska and info@. If nothing is mentioned that day, no
email is sent at all. The whole thing runs on your Mac mini, uses no cloud AI
(the summarizing is done by the same local model the main scouting system
uses), and deletes the downloaded newspaper and screenshots the moment the
email is sent (or immediately if there was nothing to report).

## Where it lives

```
press_monitor/
  epaper_client.py   → logs in, downloads today's edition (Playwright)
  scanner.py          → finds matches in the PDF, crops screenshots (PyMuPDF)
  summarizer.py        → writes the German summary (local Ollama LLM)
  emailer.py            → builds and sends the digest email (Gmail API)
  run_daily.py            → wires the four steps together, cleans up after itself
  keywords.yaml             → the watch list — plain text, no code
  README.md                  → operational setup notes
```

It is a **separate package from the rest of the VC-scouting system** on
purpose (see "Why it's kept separate" below) — it doesn't import from
`ingestion/`, `processing/`, or `api/` except for two small shared pieces:
the local LLM client and the Gmail login helper.

## Step by step

### 1. Login + download — `epaper_client.py`

**Library: Playwright** (`playwright==1.44.0`) — this is a browser
automation library. It literally drives a real, invisible ("headless")
Chrome browser: opens the e-paper login page, types in Corinna's email and
password, clicks the login button, waits for the page to load, then finds
today's edition and clicks the "Gesamtausgabe (PDF) laden" (download full
edition) button — exactly the same steps a human subscriber would do by
hand. It's needed because this e-paper isn't a normal API you can just
request data from; it's a login-protected website meant to be used through a
browser.

A few details worth knowing:
- The site shows a cookie-consent banner on first load; the code dismisses
  it automatically by looking for buttons like "Alle akzeptieren."
- Today's edition has a unique numeric ID buried in the page's HTML that
  changes every single day and can't be guessed — the code searches the
  page for today's date, finds the nearby download link, and extracts that
  ID from it dynamically rather than having a fixed URL.
- If today's edition hasn't been published yet when the 08:00 job runs, this
  step returns "nothing found" rather than erroring — and the whole run
  quietly stops there (no email, no crash).

The output of this step is one file: `edition.pdf` — the entire newspaper
issue for that day, saved temporarily to disk.

### 2. Scan for matches — `scanner.py`

**Library: PyMuPDF** (`pymupdf==1.26.5`, imported in code as `fitz`) — this
reads PDF files. The key fact that makes this whole feature possible without
OCR: the e-paper's PDF isn't just a scanned image — it has a real, selectable
text layer underneath (confirmed by testing against a real edition), the
same way you can select and copy text out of a normal PDF. That means the
code can search for words directly, instead of needing image-recognition
software to "read" a picture of a newspaper page.

What happens per page:
1. Get all the text on the page and check it against every entry in
   `keywords.yaml` (case-insensitive, literal substring match — e.g.
   "Kutter" matches "Kutter", "kutter", "KUTTER feiert...").
2. If a term is found, PyMuPDF's `search_for()` gives the exact on-page
   location (a bounding box) of that word — not just "it's somewhere in this
   50,000-character page of text."
3. From that location, the code walks outward through the page's layout
   (using `page.get_text("dict")`, which gives PyMuPDF's own understanding
   of where paragraphs/blocks are) to find the *whole article* around that
   word — the headline above it, the body text below it, staying within the
   same newspaper column. This is what makes the screenshot a crop of just
   that one article instead of the entire broadsheet page.
4. It renders that cropped region as a PNG image (the screenshot that goes
   in the email) and extracts the exact text inside that same crop (the
   excerpt that goes to the summarizer).
5. If two *different* watched terms turn out to be inside the *same* article
   region, they're merged into one match (so you get one email item, not
   two). If they're in different, unrelated articles on the same page (this
   happens — a newspaper page often has 2-3 unrelated stories), they're kept
   as two separate matches, each with its own correct excerpt/screenshot —
   this was a real bug found and fixed during testing (see "Bugs found and
   fixed" below).

Nothing is filtered out for being a "probably wrong" match at this stage —
e.g. "Reisacher" is both a partner company and a real German surname, so a
newspaper article about an unrelated person named Reisacher will also match.
That's deliberate: the system would rather show you one extra irrelevant
item than silently hide a real one. The judgment call about whether a match
is relevant is left to the summarizer (next step) and ultimately to you.

### 3. Summarize — `summarizer.py`

**Library: none new** — this reuses `reasoning/qwen_client.py`, the exact
same local LLM client the main VC-scouting pipeline uses for everything
else. The model is `qwen3:14b`, running entirely on your Mac mini through
Ollama — no data leaves the machine for this step, no OpenAI/Claude/cloud
API call.

For each match, the model is given the cropped article text and a prompt
(in German) asking it to write 3-4 concrete sentences: not "this article
mentions Kutter's anniversary" but the actual facts — dates, numbers, quotes,
what specifically happened. It's also asked to flag if the match looks like
a coincidence (e.g. the Reisacher-the-person case) rather than the real
company.

One reliability detail worth knowing about: during testing, a real call to
the model once came back as a completely empty string with no error at all
— the model just produced nothing useful that one time, and worked instantly
on retry. Because of that, `summarize_match()` never accepts a suspiciously
short/empty answer on the first try — it retries once automatically. If both
attempts fail, it falls back to just showing you a plain excerpt of the
article text (never silently sending a blank summary).

### 4. Email — `emailer.py`

**Library: Google's Gmail API client** (`google-api-python-client`,
`google-auth-oauthlib`, etc.) — via a shared helper, `ingestion/
gmail_auth.py`, that the newsletter-reading feature already used. This is
"OAuth," the same login flow you'd use to let an app "Sign in with Google" —
a one-time browser popup where you click "Allow," after which a token file
(`credentials/token.json`) lets the program send email as
`greentechhubx@gmail.com` without needing a password stored anywhere.

(This project originally tried the older/simpler approach — a Gmail "app
password" + a plain SMTP connection — but Google's automated risk-scoring
kept rejecting it even with valid credentials. Switching to real OAuth,
which is what Google itself recommends, fixed it immediately. See
`README.md`'s note about the account-name typo that was also part of that
saga.)

The email itself is built as a standard email message: German-language body
text with a short intro line + one paragraph per match (page number, matched
term(s), the summary), and each match's cropped screenshot attached as a PNG
image file. It's sent through the Gmail API's `.send()` call. If sending
fails for a real reason (not just "nothing to send"), it raises an error
loudly rather than failing silently — this is the one step where a silent
failure would mean nobody ever finds out something was caught.

### 5. Orchestration + cleanup — `run_daily.py`

This is the "glue" file that runs the four steps above in order:
download → scan → summarize each match → send (only if there's at least one
match). Whether it succeeds, fails, or finds nothing, a `finally` block
always deletes the downloaded PDF and all rendered screenshots from disk
afterward — no local copy of the full copyrighted newspaper issue is kept
around after the email is sent.

## The watch list — `keywords.yaml`

Plain text, not code. Organized into four groups:
- **organization**: GreenTech Hub, GT Hub
- **general**: Startup, Start-up, Gründer (German for "founder")
- **startups**: the 9 named GT Hub portfolio companies (polymerActive,
  summ.ai, archery, Innovativ-Lehm, Onox, clight, chargylize, HyWin, Asset
  Energy)
- **partners**: all 15 partner companies (the 4 founding shareholders —
  Alois Müller, SÜDPACK, Reisacher, Kutter — plus the 11 named partners —
  Goldhofer, DACHSER, Demmeler, BauGrund Süd, Knestel, Vensol, AVIA, CB-tec,
  Baufritz, HyWin Deutschland, e-con AG)

Editing this file to add/remove a term takes effect on the very next run —
no code change, no restart needed, because `scanner.py` reads it fresh every
time it runs.

## How the schedule actually works

**No custom scheduling code was written for this.** It uses `launchd`, the
built-in scheduler that's part of macOS itself (the same system that, for
example, makes Spotlight's background indexing or Time Machine backups run
automatically). A small configuration file,
`launchd/com.gthub.pressmonitor.plist`, tells macOS: "every day at 08:00,
run `python3 -m press_monitor.run_daily` in this folder." It's installed
into `~/Library/LaunchAgents/`, which is the standard place macOS looks for
a logged-in user's own scheduled jobs.

This runs as its **own separate service**, deliberately not bundled into the
main VC-scouting API's internal task scheduler (that one uses a Python
library called APScheduler, running *inside* the API process, for things
like the twice-weekly startup-scouting sweep). The reasoning: if the main
scouting API crashes, gets restarted, or is redeployed, the press monitor
should be completely unaffected — different process, different log files
(`logs/pressmonitor.log` / `logs/pressmonitor.error.log`), different failure
domain. That's why the launchd service is deliberately named
`com.gthub.pressmonitor` (not `com.vcscouting.*`) — visually and
functionally separate.

## What's shared with the main scouting system (and what isn't)

**Shared, deliberately:**
- The local Ollama LLM (`qwen3:14b`) — same model, same machine, just a
  different prompt for a different job.
- The Gmail OAuth token (`credentials/token.json`) — because you specifically
  asked to reuse the existing newsletter-reading Gmail account rather than
  set up a new one.

**Not shared, deliberately:**
- No database. Press monitor never touches Postgres or Qdrant (the two
  databases the scouting pipeline uses) — it has no persistent storage at
  all beyond the temporary files it deletes after every run.
- No scheduler. Runs via its own `launchd` job, not the API's internal one.
- No process. It's a short-lived script that starts, runs for a couple of
  minutes, and exits — it doesn't sit alongside the API as a long-running
  service.

## Credentials & privacy

Everything sensitive lives only in `.env`, which is in `.gitignore` and has
never been committed to git:
- `epaper_email` / `epaper_password` — Corinna's real e-paper subscriber
  login.
- `press_monitor_recipients` — the comma-separated list of who gets the
  digest (currently Corinna, Stefan, Franziska, and info@).

No credential ever appears in any file that's tracked by git — only the
non-secret setup instructions do.

## Known limitations (documented in `README.md` too)

- Only scans the **Memminger Zeitung** edition — the e-paper platform also
  publishes other regional editions (Kempten, Kaufbeuren, etc.) that aren't
  covered.
- **Terms of service**: automating login to a paid subscription is a real
  consideration that was explicitly flagged and knowingly accepted before
  building this, not an oversight.
- If the 08:00 run happens before that day's edition is actually published,
  it logs "not published yet" and simply waits for tomorrow's run rather
  than retrying later the same day.
- Matches are never auto-filtered for false positives (the Reisacher-the-
  person case) — every hit reaches a human, by design.

## Bugs found and fixed during testing (worth knowing about)

1. **Whole-page screenshots** — the first version screenshotted the entire
   newspaper page; fixed to crop to just the matched article.
2. **Shallow summaries** — the first prompt produced topic sentences
   ("mentions Kutter's anniversary") instead of real content; the prompt
   was rewritten to demand concrete facts, and live-tested to confirm it
   surfaces real numbers/names/quotes.
3. **Cross-contamination** — a page with two unrelated articles matching two
   different keywords was originally collapsed into one match using only
   the first article's content, silently misattributing the second term to
   the wrong article. Fixed by computing each match's own article region
   independently before merging/splitting.
4. **Silent empty summary** — a real LLM call once returned an empty string
   with no error; fixed with the retry-then-fallback logic described above.
5. **SMTP authentication failures**, eventually traced to a one-letter typo
   in the account name (`greentechubx` vs. the real `greentechhubx`) — fixed
   by switching to Gmail API/OAuth entirely, which sidesteps the issue since
   it authenticates via browser login rather than a typed email address.

## Manually running it / testing it

```bash
# Today's edition
python -m press_monitor.run_daily

# A specific past date
python -m press_monitor.run_daily --date 2026-08-03
```

Both print a small result summary (`status`, `date`, `match_count`, which
pages matched, who it was sent to) — this is the same thing the scheduled
08:00 run logs to `logs/pressmonitor.log`.
