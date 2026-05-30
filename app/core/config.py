"""
Application configuration via pydantic-settings.

All settings are loaded from environment variables (.env file supported).
Secrets and connection strings MUST come from env vars — never hardcode.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration singleton."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    app_name: str = "rinkel-auditor"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── Server ───────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    cors_origins: str = "*"

    # ── Supabase / PostgreSQL ────────────────────────────────────────────
    supabase_url: str = ""
    supabase_anon_key: str = ""
    database_url: str = ""

    # ── Google Drive ─────────────────────────────────────────────────────
    google_drive_folder_id: str = ""
    google_service_account_file: str = ""
    google_drive_share_with: str = ""  # email(s) to auto-share uploads

    # ── Audio Processing ─────────────────────────────────────────────────
    audio_temp_dir: str = "/tmp/rinkel-audio"
    audio_download_timeout: int = 120       # seconds
    audio_max_file_size_mb: int = 500       # reject files larger than this
    audio_retry_max_attempts: int = 3
    audio_retry_base_delay: float = 2.0     # exponential backoff base (seconds)
    audio_cleanup_after_upload: bool = True  # delete temp file after Drive upload

    # ── Whisper / OpenAI ──────────────────────────────────────────────────
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"             # OpenAI API model name
    whisper_language: str = "nl"                  # default language hint
    whisper_response_format: str = "verbose_json" # verbose_json gives segments
    whisper_temperature: float = 0.0              # 0 = deterministic
    whisper_max_file_size_mb: int = 25            # OpenAI limit is 25MB
    whisper_chunk_duration_minutes: int = 10      # chunk large files
    whisper_retry_max_attempts: int = 3
    whisper_retry_base_delay: float = 3.0         # seconds
    whisper_cost_per_minute: float = 0.006        # USD per minute (whisper-1)

    # ── Rinkel Webhook ───────────────────────────────────────────────────
    rinkel_webhook_secret: str = ""

    # ── Export ────────────────────────────────────────────────────────────
    export_dir: str = "/data/exports"
    export_batch_size: int = 100          # rows fetched per DB query
    export_max_records: int = 50000       # safety cap per export job
    export_retention_days: int = 30       # auto-cleanup after N days

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()
