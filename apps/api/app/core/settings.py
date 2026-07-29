"""Runtime configuration.

Every secret arrives through the environment. Nothing is hard-coded, nothing is
logged. `Settings` is constructed once at import time and cached.
"""

from __future__ import annotations

import functools
import json
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LlmMode = Literal["auto", "live", "stub"]


class Settings(BaseSettings):
    """Application settings, sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `model_` is a Pydantic-reserved prefix; our model-name fields need it freed.
        protected_namespaces=(),
    )

    # ---- Environment ------------------------------------------------------
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ---- Database ---------------------------------------------------------
    # Neon in production, local Postgres in dev. Both are real Postgres.
    database_url: str = "postgresql+asyncpg://ledgerlens:ledgerlens@localhost:5433/ledgerlens"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 15_000

    # ---- Claude -----------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    # "auto"  -> live when a key is present, deterministic stub otherwise.
    # "live"  -> require a key; fail fast if absent.
    # "stub"  -> never call the network (used by CI and the offline test suite).
    llm_mode: LlmMode = "auto"
    model_router: str = "claude-haiku-4-5"
    model_extractor: str = "claude-sonnet-4-6"
    llm_timeout_s: float = 90.0
    llm_max_attempts: int = 3
    llm_max_output_tokens: int = 8_000
    extraction_max_repair_attempts: int = 2

    # ---- Observability ----------------------------------------------------
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # ---- HTTP surface -----------------------------------------------------
    # `NoDecode` stops pydantic-settings JSON-decoding this before our validator
    # runs, so a plain comma-separated ALLOWED_ORIGINS in .env is accepted.
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    # Spec §7: 10 req/min/IP on the expensive ingestion path.
    rate_limit: str = "10/minute"
    # Reads are cheap and the pipeline visual polls ~1/s while a document is
    # in flight, so they get their own, much higher ceiling.
    read_rate_limit: str = "600/minute"
    # Review-queue write-backs are cheap but state-changing.
    mutation_rate_limit: str = "60/minute"
    # How many reverse proxies sit in front of this service. 0 = none (use the
    # socket peer). Set to 1 on Cloud Run / Hugging Face Spaces / Render.
    trusted_proxy_count: int = 0
    max_upload_bytes: int = 10 * 1024 * 1024
    api_port: int = 7860

    # ---- Domain rules -----------------------------------------------------
    vat_rate: float = 0.05  # UAE standard rate
    vat_absolute_tolerance: float = 0.02  # currency units, for rounding drift
    money_tolerance: float = 0.02
    duplicate_amount_tolerance_pct: float = 0.01  # 1%
    duplicate_date_window_days: int = 7
    duplicate_vendor_similarity: int = 88  # rapidfuzz token_set_ratio 0-100
    zscore_threshold: float = 2.0
    min_history_for_zscore: int = 4

    @field_validator(
        "anthropic_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_absent(cls, value: object) -> object:
        """An empty or whitespace-only secret means "not configured", not "configured to nothing".

        Deployment platforms hand you an empty string for a variable you declared
        but left blank — Render does exactly this for a `sync: false` field you
        skip. Without this, `anthropic_api_key` becomes `SecretStr("")`, every
        "is a key present?" check says yes, and the service tries to authenticate
        to Claude with nothing. The failure surfaces as an auth error on the first
        real document rather than as the obvious misconfiguration it is.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string or a JSON array.

        `NoDecode` hands us the raw environment string, so both shapes are parsed
        here. Operators reach for `a.com,b.com` far more often than valid JSON,
        and getting this wrong locks CORS open or shut in production.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                msg = f"ALLOWED_ORIGINS looks like JSON but does not parse: {exc.msg}"
                raise ValueError(msg) from exc
        return [origin.strip() for origin in stripped.split(",") if origin.strip()]

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql+asyncpg://", 1)
        elif value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not value.startswith("postgresql+asyncpg://"):
            msg = "database_url must be a PostgreSQL URL (asyncpg driver)"
            raise ValueError(msg)
        return value

    # ---- Derived ----------------------------------------------------------
    @property
    def use_live_llm(self) -> bool:
        """True when outbound Claude calls should actually happen."""
        if self.llm_mode == "stub":
            return False
        if self.llm_mode == "live":
            return True
        return self.anthropic_api_key is not None

    @property
    def langfuse_enabled(self) -> bool:
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None

    @property
    def sqlalchemy_url(self) -> str:
        """Connection URL with driver-level options that asyncpg cannot take in the DSN."""
        # Neon requires TLS; asyncpg reads `ssl` from connect_args, not the query string,
        # so strip libpq-style params that asyncpg would reject.
        base, _, query = self.database_url.partition("?")
        if not query:
            return self.database_url
        keep = [p for p in query.split("&") if not p.startswith(("sslmode=", "channel_binding="))]
        return f"{base}?{'&'.join(keep)}" if keep else base

    @property
    def database_requires_tls(self) -> bool:
        return "sslmode=require" in self.database_url or ".neon.tech" in self.database_url


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
