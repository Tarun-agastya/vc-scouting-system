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

    # Gmail
    gmail_credentials_path: str = "./credentials/gmail_credentials.json"

    # Press monitor (Phase PM, 4 Aug 2026) — daily Allgäuer/Memminger Zeitung
    # e-paper scan for GreenTech Hub / portfolio / partner mentions.
    # epaper_* are a real subscriber login (Corinna's), never committed.
    # Sending reuses the existing newsletter Gmail account via SMTP + an app
    # password — NOT the same as gmail_credentials_path, which is an OAuth
    # token scoped gmail.readonly only (ingestion/newsletter_ingestor.py) and
    # cannot send mail.
    epaper_email: Optional[str] = None
    epaper_password: Optional[str] = None
    epaper_region: str = "ME"       # Memmingen — the Memminger Zeitung edition
    epaper_publication: str = "Memminger Zeitung"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: Optional[str] = None          # the sending Gmail address
    smtp_app_password: Optional[str] = None  # Google Account app password, not the login password
    press_monitor_recipients: str = "corinna.tappe@gt-hub.de,stefan.lenz@gt-hub.de"  # comma-separated

    # Tavily (Phase W web verification search backend, 24 Jul — replaces the
    # DuckDuckGo HTML scrape, which an anti-bot block made unreliable; see
    # ingestion/web_search.py's module docstring for the full evaluation).
    # Optional — if unset, ingestion.web_search falls back to DuckDuckGo.
    tavily_api_key: Optional[str] = None

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
    # Phase R-4 kill switch. False (default) = every page uses
    # PageStrategy.DEFAULT, reproducing today's pipeline byte-for-byte —
    # SiteProfile is populated (R-1/R-2/R-3) but nothing reads it into the
    # live crawl/chunk/extract path yet. True threads the learned/adjudicated
    # strategy through fetch → chunk → extract for real.
    adaptive_pipeline_enabled: bool = False
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
    dedup_weight_name: float = 0.30       # name string similarity
    dedup_weight_embedding: float = 0.30  # whole-record embedding (semantic) similarity
    dedup_weight_location: float = 0.15   # city/country agreement
    dedup_weight_founded_year: float = 0.10
    dedup_weight_founders: float = 0.15   # founder-name overlap
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
    # Pattern-decision thresholds (evidence patterns, not a single linear gate)
    dedup_strong_signal: float = 0.80     # a per-signal value >= this counts as "strong"
    dedup_anomaly_gap: float = 0.30       # domain strong but best other signal below this -> anomaly

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
