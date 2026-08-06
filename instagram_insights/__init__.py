"""
Instagram business-account insights (Phase IG, 6 Aug 2026).

Monthly + quarterly reporting on OUR OWN company Instagram Business account,
via the OFFICIAL Meta Graph API. Standalone module in the press_monitor/ mould:
its own launchd jobs, its own Postgres tables, its own logs. Shares only the
Gmail send capability (ingestion/gmail_auth.py) for delivering the reports.
Touches nothing in the VC-scouting pipeline.

SAFETY — the governing constraint for this whole module (owner, 6 Aug):
  * READ-ONLY by construction. The token carries instagram_basic +
    instagram_manage_insights + pages_read_engagement and no write scope, so it
    is not capable of posting, commenting, following or messaging.
  * NEVER scrape or browser-automate instagram.com. That is the one realistic
    path to a business account being restricted or disabled, and it is
    prohibited outright. Playwright appears in this module solely to render our
    own local HTML report file to an image — never to visit Instagram.
  * NEVER call undocumented endpoints (i.instagram.com, /api/v1/...), never
    store the account password, never add a third-party scraper dependency.

See README.md in this directory for the full rule list, the one-time Meta
setup, and the reasoning behind daily collection despite monthly reporting.
"""
