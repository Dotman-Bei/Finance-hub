"""Single loader for the .env keys in build.md §4.

Every service imports `settings` from here rather than calling os.getenv, so a
missing or malformed key fails once, loudly, at import — not deep inside a
reconciliation run.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("FINANCEHUB_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── PostgreSQL ───────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+psycopg2://financehub:changeme@postgres:5432/financehub"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── Redis ────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"

    # ── Kafka ────────────────────────────────────────────────────────────
    kafka_broker: str = "kafka:9092"
    kafka_topic_raw: str = "raw_transactions"

    # ── Reconciliation tuning ────────────────────────────────────────────
    match_confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    retrain_trigger_count: int = Field(default=200, gt=0)

    # ── Auth ─────────────────────────────────────────────────────────────
    jwt_secret: str = "change-this"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    @field_validator("database_url")
    @classmethod
    def must_be_postgres(cls, v: str) -> str:
        # Postgres is the single source of truth (§1). Anything else means a
        # service is quietly writing somewhere the other subsystems can't read.
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a postgresql:// URL")
        return v


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - startup failure path
        raise RuntimeError(
            f"Invalid FinanceHub configuration. Check your .env against "
            f".env.example.\n{exc}"
        ) from exc


settings = get_settings()

__all__ = ["Settings", "settings", "get_settings"]
