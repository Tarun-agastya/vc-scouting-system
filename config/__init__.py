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

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # Worker Queue
    max_qwen_workers: int = 1    # Raise via env MAX_QWEN_WORKERS; keep at 1 on Mac Mini
    page_queue_size: int = 5     # Max pages buffered between crawler and chunker
    chunk_queue_size: int = 20   # Max chunks buffered between chunker and Qwen workers
    storage_queue_size: int = 50 # Max startup dicts buffered before storage

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
