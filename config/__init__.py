"""
Re-exports Settings so that 'from config import settings' continues to work
now that config/ is a package (which shadows the root-level config.py).
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://scout:scoutpass123@localhost:5432/vc_scouting"

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_reason_model: str = "qwen3:14b"
    ollama_extract_model: str = "qwen2.5:7b-instruct"  # hot-path extraction; override via OLLAMA_EXTRACT_MODEL

    # Discord
    discord_bot_token: Optional[str] = None

    # Gmail (IMAP/SMTP via App Password — Phase PM, 12 Aug 2026)
    #
    # Switched from OAuth (see git history, commit 72713ae) after the OAuth
    # client's Testing publishing status turned out to force a full
    # interactive browser re-consent every 7 days regardless of activity —
    # workable for a human-driven tool, not for an unattended 8am launchd
    # job. Moving to Google's verified/Production OAuth tier would need a
    # paid CASA security assessment for gmail.readonly (a Restricted scope,
    # not just Sensitive) — disproportionate for a single dummy account.
    # App Passwords sidestep Google's OAuth consent machinery entirely: no
    # Testing/Production split, no scheduled expiry, no browser click, ever,
    # until the password is manually revoked.
    #
    # (The ORIGINAL app-password attempt that motivated the OAuth switch was
    # misdiagnosed at the time as "Google's risk-scoring" — git history for
    # commit 72713ae shows the real cause was a one-letter typo in the
    # configured address, nothing wrong with app passwords themselves.)
    #
    # Requires 2-Step Verification enabled on the account, then an App
    # Password generated at myaccount.google.com/apppasswords. Still used by
    # gmail_credentials_path? No — that file/path is now unused; kept only
    # so an old .env pointing at it doesn't hard-fail on a missing setting.
    gmail_credentials_path: str = "./credentials/gmail_credentials.json"
    gmail_address: Optional[str] = None       # the account's own address
    gmail_app_password: Optional[str] = None  # 16-char App Password, NOT the login password
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 465  # SMTP over SSL

    # Press monitor (Phase PM, 4 Aug 2026) — daily Allgäuer/Memminger Zeitung
    # e-paper scan for GreenTech Hub / portfolio / partner mentions.
    # epaper_* are a real subscriber login (Corinna's), never committed.
    # Sending reuses the gmail_address/gmail_app_password above — see
    # ingestion/gmail_auth.py.
    epaper_email: Optional[str] = None
    epaper_password: Optional[str] = None
    epaper_region: str = "ME"       # Memmingen — the Memminger Zeitung edition
    epaper_publication: str = "Memminger Zeitung"
    press_monitor_recipients: str = "corinna.tappe@gt-hub.de,stefan.lenz@gt-hub.de"  # comma-separated

    # ── Instagram business-account insights (Phase IG, 6 Aug 2026) ───────────
    # Monthly/quarterly reporting on OUR OWN company Instagram account, via
    # the OFFICIAL Meta Graph API only. Read-only by construction: the token
    # carries instagram_basic + instagram_manage_insights + pages_read_engagement
    # and NOTHING else, so it is not capable of posting, commenting, following
    # or messaging even if code tried. Scraping / browser-automating
    # instagram.com is the one real path to a business account being
    # restricted, and is prohibited outright — see instagram_insights/README.md.
    #
    # Default OFF: the owner's staged-rollout requirement. Nothing collects
    # until this is explicitly flipped on after a manual probe + manual run
    # have both been verified. Also the kill switch — set False to stop all
    # collection without uninstalling the launchd jobs.
    ig_enabled: bool = False
    ig_token_path: str = "./credentials/instagram_token.json"  # gitignored via credentials/*.json
    # "instagram_login" (default): "Business Login for Instagram" — the
    # account authenticates directly, no Facebook Page needed. Confirmed 6 Aug
    # 2026 to be this project's actual account (Instagram-only, no linked
    # Page). Data host graph.instagram.com; tokens last 60 days and MUST be
    # refreshed (no never-expire System User option without a Page).
    # "facebook_login": the older Page-mediated path (graph.facebook.com),
    # which DOES support a never-expiring Business Manager System User token
    # — only relevant if a Facebook Page is linked later.
    ig_auth_mode: str = "instagram_login"
    # Pinned by scripts/ig_probe.py on its first successful run so the daily
    # job never has to re-resolve it (one fewer call, one fewer failure mode).
    ig_user_id: Optional[str] = None
    # Graph API version. Metric names churn between versions (impressions and
    # video_views were both retired in favour of `views` in v22.0), so this is
    # pinned deliberately rather than tracking "latest" — an unannounced
    # rename would otherwise silently blank a column. ig_probe.py validates it.
    ig_api_version: str = "v23.0"
    # Refresh a long-lived user token once it is this many days old. Meta's
    # long-lived tokens last 60 days and can only be refreshed while still
    # valid, so 30 leaves a full month of slack for an unattended machine.
    # Irrelevant (and skipped) if a Business-Manager System User token is used,
    # which never expires — that is the recommended setup for this box.
    ig_token_refresh_days: int = 30
    # Hard per-run cap on Graph API calls. Meta allows 200/hour/account; a
    # daily collection needs ~10-20. This is a safety net against a loop bug
    # turning into rate-limit abuse, following the same "one blunt per-run
    # cap" pattern as web_verify_chain_limit above.
    ig_max_calls_per_run: int = 60
    ig_report_recipients: str = ""  # comma-separated; empty = report emailing disabled

    # ── Google Places (Phase RC-3, regional discovery) ───────────────────────
    # The ONLY paid data source in this project. Optional: without a key the
    # regional register still works from Wikidata + Wikipedia + OSM, it just
    # reaches lower recall. Pro-tier fields cost ~$32 per 1000 calls, so a full
    # 50 km sweep is roughly $20-30 — always run the estimate first.
    google_places_api_key: Optional[str] = None
    # Hard stop on calls per sweep. Like web_verify_chain_limit for Tavily,
    # this batch cap is the only cost control that exists — there is no quota
    # accounting anywhere. Raise it deliberately, never by reflex.
    google_places_max_calls: int = 200
    # Tile radius in km. Smaller = better coverage in dense towns, more calls.
    google_places_tile_km: float = 4.0

    # Tavily (Phase W web verification search backend, 24 Jul — replaces the
    # DuckDuckGo HTML scrape, which an anti-bot block made unreliable; see
    # ingestion/web_search.py's module docstring for the full evaluation).
    # Optional — if unset, ingestion.web_search falls back to SearXNG/DuckDuckGo.
    tavily_api_key: Optional[str] = None

    # SearXNG (11 Aug 2026) — self-hosted, free web-search fallback used
    # BETWEEN Tavily and the DuckDuckGo scrape. Runs as the `searxng` service
    # in docker-compose.yml; see the comment there and
    # ingestion/web_search.py for why it exists. No API key: it's our own
    # container. Only the base URL is configurable (e.g. if the port is ever
    # remapped, or a shared instance is used instead of the local one).
    searxng_url: str = "http://localhost:8888"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # Worker Queue
    max_qwen_workers: int = 1    # Raise via env MAX_QWEN_WORKERS; keep at 1 on Mac Mini
    page_queue_size: int = 5     # Max pages buffered between crawler and chunker
    chunk_queue_size: int = 20   # Max chunks buffered between chunker and Qwen workers
    storage_queue_size: int = 50 # Max startup dicts buffered before storage

    # Crawl reach (Phase 23 Jul — coverage/recall fix). The old hardcoded
    # max_pages=10 was spent on top-level nav before the crawler ever reached a
    # startup profile (diagnosed via scripts/probe_page.py). Raised + made the
    # frontier priority-ordered so detail/portfolio pages are visited FIRST.
    crawl_max_pages: int = 25     # pages per source per run (was a hardcoded 10)
    crawl_max_depth: int = 3      # link-hops from the entry URL (was a hardcoded 2)
    # Max "load more" clicks / infinite-scroll steps on a rendered directory
    # page. Each click loads more of the list but adds extraction volume, so
    # this is the completeness-vs-speed knob. munich-startup's full startup
    # list needs ~29 clicks (10K → 350K chars); default is a middle ground.
    crawl_max_load_more: int = 15

    # Adaptive pipeline (Phase R, 31 Jul — self-adapting web extraction).
    # How long a learned SiteProfile is trusted before a fresh probe is due,
    # so a site redesign is eventually noticed even if the recall audit
    # (Phase R-5) never sees the specific page again. status="pinned"
    # profiles are exempt — the manual override that replaces per-site
    # hand-tuning never expires on its own.
    site_profile_ttl_days: int = 30
    # Phase R-3 kill switch. The deterministic structural strategy (Phase
    # R-1) always runs first and is never blocked on this — the LLM only
    # adjudicates it, so turning this off just means every profile stays
    # deterministic-only, exactly like before R-3 shipped.
    site_strategy_llm_enabled: bool = True
    # Phase R-4 kill switch. False = every page uses PageStrategy.DEFAULT,
    # reproducing the pre-Phase-R pipeline byte-for-byte (SiteProfile still
    # populated but unread). True threads the learned/adjudicated structural
    # strategy through fetch → chunk → extract for real. Turned on by
    # default 4 Aug — R-0 through R-7 were built, tested, and verified
    # (validation/R*_VERIFICATION.md) specifically to replace the blind
    # "≥12 alt texts = logo grid" heuristic with real per-page layout
    # analysis; a hochschule-biberach.de crawl on the OLD (off) path is what
    # produced ~124 fake "startups" from a photo carousel the same week —
    # exactly the failure class this flag exists to prevent. Set False in
    # .env to fall back to the old behaviour if a live source ever regresses.
    adaptive_pipeline_enabled: bool = True
    # Phase R-5 — recall audit + auto-retry. A page is a "shortfall" when it
    # expected entities, actual recall fell below this ratio, AND the
    # absolute gap clears recall_shortfall_min_gap (the floor stops thrash
    # on small pages, where a 1-of-3 miss is a huge ratio but a trivial
    # absolute loss). Phase B retries at most recall_retry_max_pages worst
    # offenders, spending at most recall_retry_max_calls added Qwen calls
    # across all of them combined — a hard ceiling so a bad retry can't
    # silently double a run's cost.
    recall_shortfall_ratio: float = 0.6
    recall_shortfall_min_gap: int = 5
    recall_retry_max_pages: int = 3
    recall_retry_max_calls: int = 30
    # Phase R-6 — detail-page following. A name-only logo grid's per-company
    # detail links come out of the SAME max_pages budget as everything else
    # (never a separate allowance), capped at this share so enrichment can
    # never starve the listing crawl itself of its own page budget.
    crawl_detail_page_share: float = 0.6

    # Deduplication / entity matching (Phase S-3 — multi-signal matcher)
    # All tunable via .env so matching behaviour can be calibrated against real
    # data without a code change. Weights should sum to ~1.0.
    dedup_block_top_n: int = 10           # candidates pulled from Qdrant for scoring
    dedup_merge_threshold: float = 0.82   # weighted score >= this  -> auto-merge
    dedup_review_threshold: float = 0.55  # in [review, merge)       -> flag for human review
    # Location carries ZERO weight (owner decision, 4 Aug). Many genuinely
    # different startups share a city/country simply by being in the same hub
    # (Munich, Berlin, Zurich), so location agreement is not identity evidence.
    # It was first removed from _classify's num_strong pattern-count, but kept
    # leaking through THIS weighted aggregate: 0.15 stacked with a
    # generically-elevated embedding score (same-industry vocabulary) was still
    # enough to cross dedup_review_threshold and stage a review for two
    # completely differently-named companies. Still computed and shown in each
    # review's `evidence` for human context — just never scored.
    #
    # The other four weights are RENORMALIZED to sum to 1.0 (from the original
    # 0.30/0.30/0.10/0.15, which summed to 0.85 once location's 0.15 was
    # dropped). Without this the weights silently violated the "should sum to
    # ~1.0" rule above and dedup_review_threshold=0.55 became an effective
    # 0.647 bar — measured live: genuine duplicates like "Quantum Diamonds ~
    # QuantumDiamonds" and "Bugsense Diagnostics ~ Bugsense" fell below it and
    # reclassified as no_match. Renormalizing keeps each remaining signal's
    # RELATIVE importance identical to before and leaves the threshold
    # meaning exactly what it always meant.
    dedup_weight_name: float = 0.353       # name string similarity
    dedup_weight_embedding: float = 0.353  # whole-record embedding (semantic) similarity
    dedup_weight_location: float = 0.0
    dedup_weight_founded_year: float = 0.118
    dedup_weight_founders: float = 0.176   # founder-name overlap
    dedup_llm_judge: bool = False         # legacy inline judge — kept off; Layer 4 is now async explanation only

    # Phase S-3b — data-stewardship matcher
    # Multi-tenant / shared domains that must NOT be treated as an identity
    # signal (many unrelated startups share them). Comma-separated in .env.
    dedup_multitenant_domains: str = (
        "linkedin.com,twitter.com,x.com,facebook.com,instagram.com,medium.com,"
        "substack.com,beehiiv.com,notion.site,notion.so,github.io,github.com,"
        "webflow.io,linktr.ee,youtube.com,crunchbase.com,angel.co,wefunder.com,"
        "eu-startups.com,gmail.com,google.com"
    )
    # Phase X-4 (5 Aug): cap on how many name-only stubs one chained
    # post-ingestion web-verify pass may process. Each record costs exactly one
    # outbound search call, and ingestion/web_search.py has NO rate limiting,
    # quota accounting or daily cap of any kind — this batch limit is the only
    # cost control that exists, so a single large logo-grid source must not be
    # able to fire hundreds of searches. The nightly 04:00 job remains the
    # backlog drain; this chain exists for freshness, not throughput.
    web_verify_chain_limit: int = 25

    # Pattern-decision thresholds (evidence patterns, not a single linear gate)
    dedup_strong_signal: float = 0.80     # a per-signal value >= this counts as "strong"
    dedup_anomaly_gap: float = 0.30       # domain strong but best other signal below this -> anomaly

    # Literal description-overlap VETO (4 Aug). Embedding similarity alone is
    # too forgiving between two different companies that merely share industry
    # vocabulary ("AI-powered platform for X") — measured live on the real
    # pending backlog, pairs like "Vaeridion ~ Bai Soft GmbH" scored emb 0.78
    # with ZERO literal description overlap. When two descriptions share no
    # literal content AND the names differ, that is decisive evidence they are
    # NOT the same company, whatever the embedding says.
    #
    # Deliberately a veto only, never a positive signal (hence no weight in the
    # aggregate score): the same measurement found the HIGH-overlap end is not
    # duplicates either — pairs like "Collimate Health ~ Causa Prima" share a
    # 1.0-identical description with completely different names, which is the
    # extraction cross-attribution bug, not identity. Scoring high overlap UP
    # would have made those worse.
    dedup_text_overlap_veto: float = 0.05        # overlap below this = no shared literal content
    dedup_text_overlap_name_ceiling: float = 0.5 # ...and names below this = veto the pair

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
