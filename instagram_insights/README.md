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

- **Never** point Playwright, Selenium, `httpx`, `requests`, or anything else at
  `instagram.com` or `www.instagram.com`. Playwright is used in this module for
  exactly one thing: rendering our own local HTML report file to an image. It
  must never visit Instagram.
- **Never** request a write scope (`instagram_content_publish`,
  `instagram_manage_comments`, `instagram_manage_messages`).
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

There is a standing verification for the first rule — it looks for real URLs
rather than prose mentions, so it should return **nothing at all**:

```bash
grep -rnE "https?://[a-z0-9.-]*instagram\.com" instagram_insights/ scripts/ig_probe.py
```

Any hit means something is pointed at the Instagram website instead of the
Graph API. Run it before every commit that touches this module.

---

## 2. Prerequisites (manual, one time, ~30–45 min in Meta's UI)

Nothing in this module works until all of these are true.

### 2.1 Account type
The Instagram account must be a **Business** or **Creator** account.
Personal accounts have **no** API access at all — this is not a limitation we
can work around.

*Check:* Instagram app → Settings → Account type and tools.

### 2.2 Linked Facebook Page
The Instagram account must be **linked to a Facebook Page**, and you must be
**admin of both**.

*Check:* Facebook Page → Settings → Linked accounts → Instagram.

### 2.3 Meta app (leave it in Development mode)
1. Go to <https://developers.facebook.com/apps> → **Create App**.
2. Type: **Business**.
3. Add the **Instagram Graph API** product.
4. **Leave the app in "Development" mode.** Do not publish it, and do not
   request App Review.

> **Why no App Review?** App Review + Business Verification (2–4 weeks) is only
> needed for *Advanced Access* — reading accounts you do **not** own. We are
> reading our own account, where you are admin, which is covered by *Standard
> Access* in Development mode. This is the single biggest reason this project
> takes a day instead of a month.

### 2.4 Token — use a System User token if at all possible

**Option A — System User token (strongly recommended).**
Meta Business Manager → Business Settings → Users → **System Users** → Add →
Assign the Facebook Page asset → **Generate new token** → select your app →
choose these three permissions and *nothing else*:

- `instagram_basic`
- `instagram_manage_insights`
- `pages_read_engagement`

Set the expiry to **Never**.

This is recommended because it removes the entire 60-day token-expiry failure
mode. The Mac mini runs unattended for weeks at a time; a token that quietly
dies mid-holiday means permanently lost data, because Instagram only retains
insights for ~90 days and never backfills.

**Option B — long-lived user token (fallback).**
Only if Business Manager isn't available. Lasts 60 days and must be refreshed
while still valid; `instagram_insights/auth.py` handles that automatically, but
it is strictly more fragile than Option A.

### 2.5 Save the token

```bash
# From the project root. Do NOT paste the token into a tracked file.
cat > credentials/instagram_token.json <<'JSON'
{
  "access_token": "PASTE_TOKEN_HERE",
  "token_type": "system_user",
  "issued_at": "2026-08-06T00:00:00Z"
}
JSON
chmod 600 credentials/instagram_token.json
```

`credentials/*.json` is already in `.gitignore`, so this cannot be committed by
accident. Use `"token_type": "long_lived_user"` instead if you used Option B —
that is what tells `auth.py` whether refreshing is needed at all.

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
for this account. Two are worth expecting:

- **Audience demographics** (age / gender / city / country) require **100+
  followers**. Below that, Meta returns nothing at all.
- **`profile_views`** may have been retired at account level. The probe reports
  the truth rather than guessing.

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
