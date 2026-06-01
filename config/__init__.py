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

    # Discord
    discord_bot_token: Optional[str] = None

    # Gmail
    gmail_credentials_path: str = "./credentials/gmail_credentials.json"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
