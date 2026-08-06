# Instagram Insights — setup & safety

Automated monthly + quarterly reporting on **our own** company Instagram Business
account, using the **official Meta Graph API**.

---

## 1. Safety first — read this before anything else

**This system is strictly READ-ONLY.** It never posts, comments, likes, follows,
unfollows, or sends messages. It cannot: the access token is issued with three
*read* permissions and no write permission, so a write call would be rejected by
Meta even if some future code tried to make one.

That matters because Meta's enforcement against automation is overwhelmingly
aimed at **write** behaviour — engagement bots, mass-following, spam — and at
**scraping the website**. Reading your own account's insights through the
official API with a properly issued token is the exact thing that API exists for.

### Hard rules — binding on anyone (human or AI) editing this module

- **Never** write CODE that points Playwright, Selenium, `httpx`, `requests`,
  or anything else at `instagram.com` / `www.instagram.com`. Playwright is
  used in this module for exactly one thing: rendering our own local HTML
  report file to an image. It must never visit Instagram. (The one narrow
  exception is §2.4's App-Dashboard token popup — a human clicking through
  Meta's own consent screen once, not code. See that section for why this
  isn't a contradiction.)
- **Never** request a write scope (`instagram_business_content_publish`,
  `instagram_business_manage_comments`, `instagram_business_manage_messages`,
  or their older `instagram_manage_*` equivalents).
- **Never** store the Instagram account password. Anywhere. In any form.
- **Never** call an undocumented endpoint (`i.instagram.com`, `/api/v1/…`).
  Those are what unofficial "Instagram API" libraries use, and they are exactly
  what Meta polices.
- **Never** add a third-party Instagram scraper package as a dependency,
  however convenient it looks.
- **Never** retry hard on error `4`/`17` (rate limit) or `190` (bad token) —
  back off and alert. Hammering after a rejection is what turns throttling into
  enforcement.
- **Never** commit or log the token.

There is a standing verification for the first rule, scoped to **code**, not
this doc's own prose about the Dashboard flow — it should return **nothing**:

```bash
grep -rnE "https?://[a-z0-9.-]*instagram\.com" instagram_insights/*.py scripts/ig_probe.py
```

Any hit means code — not a human in a browser — is pointed at the Instagram
website instead of the Graph API. Run it before every commit that touches
this module.

---

## 2. Prerequisites (manual, one time, ~30–45 min in Meta's UI)

Nothing in this module works until all of these are true.

### 2.1 Account type
The Instagram account must be a **Business** or **Creator** account.
Personal accounts have **no** API access at all — this is not a limitation we
can work around.

*Check:* Instagram app → Settings → Account type and tools.

### 2.2 Facebook Page — not required for this account

**Confirmed 6 Aug 2026: this account has no linked Facebook Page**, so the
setup below uses Meta's **"Business Login for Instagram"** path, which
authenticates directly against the Instagram account and needs no Page and no
Business Manager. (§2.2 in the original draft of this doc assumed a Page —
that assumption was wrong for this account and has been corrected here. If a
Page ever gets linked later, the alternative path in §2.6 becomes available
and unlocks a token that never expires — worth revisiting then, not now.)

### 2.3 Meta app (leave it in Development mode)
1. Go to <https://developers.facebook.com/apps> → **Create App**.
2. Type: **Business**.
3. Add the **Instagram** product (specifically "API setup with Instagram
   login" inside it — this is the no-Page path).
4. **Leave the app in "Development" mode.** Do not publish it, and do not
   request App Review.

> **Why no App Review?** App Review + Business Verification (2–4 weeks) is only
> needed for *Advanced Access* — reading accounts you do **not** own. We are
> reading our own account, where you are admin, which is covered by *Standard
> Access* in Development mode. This is the single biggest reason this project
> takes a day instead of a month.

### 2.4 Generate the token — via the App Dashboard's built-in flow

No coding, no OAuth redirect server needed — Meta's dashboard does this
entirely in the browser:

1. In your app, left sidebar → **Instagram** → **API setup with Instagram
   login**.
2. Under **Generate access tokens**, click **Add an Instagram Account**.
3. A login popup opens — sign in with the Instagram Business/Creator account
   itself and click **Allow** when it asks you to grant access.
4. Copy the generated **access token**.

**This one popup is the only moment anything Instagram-related happens in a
browser, and it is you, a human, clicking through Meta's own official consent
screen** — the same shape as the Gmail OAuth consent this project already
uses (`ingestion/gmail_auth.py`). It is categorically different from, and not
in conflict with, the "never automate instagram.com" rule in §1 — that rule
is about *code* silently visiting Instagram, never about a person consenting
through Meta's own sanctioned UI once.

When granting access, confirm only these permissions are requested — this is
what makes the token physically incapable of writing anything:

- `instagram_business_basic`
- `instagram_business_manage_insights`

Do **not** grant `instagram_business_content_publish`,
`instagram_business_manage_comments`, or `instagram_business_manage_messages`
— none of them are needed and granting them would give the token capabilities
this project must never use.

> **No never-expiring option here.** Without a linked Facebook Page, there is
> no Business Manager System User token — that path is Page-only (§2.6). This
> token lasts **60 days** and must be refreshed before it dies; that refresh
> is automatic once IG-1 (`instagram_insights/auth.py`) is built (default:
> refresh at day 30, well inside the window). Until then, note the issue date
> so a 60-day gap doesn't sneak up unattended.

### 2.5 Save the token

```bash
# From the project root. Do NOT paste the token into a tracked file.
cat > credentials/instagram_token.json <<'JSON'
{
  "access_token": "PASTE_TOKEN_HERE",
  "token_type": "instagram_login_long_lived",
  "issued_at": "2026-08-06T00:00:00Z"
}
JSON
chmod 600 credentials/instagram_token.json
```

`credentials/*.json` is already in `.gitignore`, so this cannot be committed by
accident.

### 2.6 If a Facebook Page gets linked later (not this project's setup today)

The alternative "Facebook Login" path becomes available: Meta Business
Manager → Business Settings → Users → **System Users** → Add → assign the
Page asset → generate a token scoped to `instagram_basic` +
`instagram_manage_insights` + `pages_read_engagement`, expiry **Never**. That
removes the 60-day refresh entirely. Switch `ig_auth_mode` from
`instagram_login` to `facebook_login` in `.env` if this is ever done — nothing
else in this module needs to change; the host and account-lookup call both
branch on that one setting (`scripts/ig_probe.py::resolve_account`).

---

## 3. Step 1: run the probe (read-only, safe to run any time)

```bash
python3 scripts/ig_probe.py
```

This makes **read-only** calls and writes **nothing** to Instagram and nothing to
our database. It exists because Meta renames metrics between API versions
(`impressions` and `video_views` were both retired in v22.0 in favour of
`views`), so rather than trusting documentation we ask *this specific account*
what it actually returns.

It produces `instagram_insights/metric_support.json` and prints a summary,
including — importantly — **which of the requested metrics are NOT available**
for this account. Confirmed 6 Aug 2026: this account has >100 followers, so
the 100-follower demographics floor is already cleared and isn't expected to
be an issue. `profile_views` at account level is the one still worth watching
— it may have been retired in a recent API version; the probe reports the
truth rather than guessing.

Read that output before we build anything on top of it.

---

## 4. What happens after the probe

Only once the probe output has been reviewed:

| Phase | What it does |
|---|---|
| IG-1 | Auth + API client (token refresh, per-run call budget, error handling) |
| IG-2 | Three Postgres tables + migration |
| IG-3 | Daily collector + its launchd job |
| IG-4 | Monthly/quarterly report builder + email delivery |
| IG-5 | Fail-loud health guard (alerts before the token dies) |

`ig_enabled` in `config/__init__.py` defaults to **False**. Nothing collects
automatically until it is explicitly turned on, after a manual run has been
verified. It also doubles as the kill switch.

---

## 5. Why daily collection, when the reports are monthly?

Instagram keeps insights for about **90 days** and never backfills. A quarter is
~90–92 days — right at that boundary. If a quarterly fetch ran late or failed
once, that quarter would be gone permanently.

So collection and reporting are decoupled: a small daily job stores metrics in
our own Postgres, and the monthly/quarterly reports are computed from *our*
data, never re-fetched from Instagram. Each run also re-pulls the trailing three
days and upserts, which absorbs Instagram's up-to-48-hour reporting delay on
demographics **and** means a run missed while nobody was watching is
automatically backfilled by the next successful one.

A consequence worth knowing: once the daily job has been running, reports are
reproducible forever and are never blocked by a token problem or a Meta outage.

---

## 6. Relationship to the rest of this repo

This is a **standalone module**, deliberately following the `press_monitor/`
pattern: its own launchd jobs (`com.gthub.instagram-*`), its own logs, its own
tables. It shares only the Gmail send capability
(`ingestion/gmail_auth.py`) for delivering reports.

It has **nothing** to do with the VC-scouting pipeline. It does not touch
`startups`, Qdrant, Ollama, or the scouting API — a crash, redeploy, or restart
of either one cannot affect the other.
