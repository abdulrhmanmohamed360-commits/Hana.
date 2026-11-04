"""Environment-based configuration. No secrets are hardcoded; everything comes from env vars."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "EduForge Knowledge Core"
    app_version: str = "0.1.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://eduforge:eduforge@localhost:5432/eduforge"
    redis_url: str = "redis://localhost:6379/0"

    admin_api_key: SecretStr | None = None
    editor_api_key: SecretStr | None = None
    reader_api_key: SecretStr | None = None

    # OpenAI-compatible model endpoint; optional until configured by the operator.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 90

    # Optional Tavily search integration; search results remain unverified leads.
    tavily_api_key: SecretStr | None = None
    research_timeout_seconds: int = 30

    # Embeddings (OpenAI-compatible /embeddings). Dimensions are fixed at 1536 by the schema.
    embedding_base_url: str | None = None          # defaults to llm_base_url
    embedding_api_key: SecretStr | None = None     # defaults to llm_api_key
    embedding_model: str = "text-embedding-3-small"
    embedding_timeout_seconds: int = 30

    # Continuous learning: hard cost/request limits for gap-driven external research
    learning_max_research_per_scan: int = 3
    learning_max_research_per_day: int = 10
    learning_max_gaps_per_scan: int = 200
    learning_max_assets_assessed_per_scan: int = 100

    # Knowledge preparation
    prepare_max_evidence_chunks: int = 8
    prepare_job_max_attempts: int = 3

    rate_limit_backend: Literal["redis", "memory"] = "redis"
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    allow_private_networks: bool = False
    max_download_bytes: int = 25 * 1024 * 1024
    fetch_timeout_seconds: int = 20
    storage_dir: str = "./storage/documents"
    max_upload_bytes: int = 25 * 1024 * 1024

    default_page_size: int = 20
    max_page_size: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
