"""
Trend-Investorr: Configuration Settings

Pydantic-based settings with .env file loading, type validation,
and immutable configuration after initialization.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from core.exceptions import ConfigurationError


class AppEnvironment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """
    Application settings loaded from .env file.

    Static settings (secrets, connection strings) live here.
    Runtime-tunable settings live in the app_config DB table.
    """

    # ── App ──────────────────────────────────────────────
    APP_ENV: AppEnvironment = AppEnvironment.DEVELOPMENT
    LOG_LEVEL: str = "INFO"
    BASE_DIR: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    # ── Angel One SmartAPI ───────────────────────────────
    ANGEL_API_KEY: str = ""
    ANGEL_CLIENT_ID: str = ""
    ANGEL_PASSWORD: str = ""
    ANGEL_TOTP_SECRET: str = ""

    # ── Google Gemini AI (optional) ──────────────────────
    GEMINI_API_KEY: str = ""

    # ── PostgreSQL Database ──────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "trend_investorr"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_MIN_CONNECTIONS: int = 2
    DB_MAX_CONNECTIONS: int = 10

    # ── Vault ────────────────────────────────────────────
    VAULT_MASTER_KEY: str = ""

    # ── Validators ───────────────────────────────────────

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got '{v}'")
        return upper

    @field_validator("DB_PORT")
    @classmethod
    def validate_db_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"DB_PORT must be 1-65535, got {v}")
        return v

    @field_validator("DB_MIN_CONNECTIONS", "DB_MAX_CONNECTIONS")
    @classmethod
    def validate_pool_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"Connection pool size must be >= 1, got {v}")
        return v

    @model_validator(mode="after")
    def validate_pool_range(self) -> "Settings":
        if self.DB_MIN_CONNECTIONS > self.DB_MAX_CONNECTIONS:
            raise ValueError(
                f"DB_MIN_CONNECTIONS ({self.DB_MIN_CONNECTIONS}) cannot exceed "
                f"DB_MAX_CONNECTIONS ({self.DB_MAX_CONNECTIONS})"
            )
        return self

    # ── Computed Properties ──────────────────────────────

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == AppEnvironment.PRODUCTION

    @property
    def has_angel_credentials(self) -> bool:
        return all([
            self.ANGEL_API_KEY,
            self.ANGEL_CLIENT_ID,
            self.ANGEL_PASSWORD,
            self.ANGEL_TOTP_SECRET,
        ])

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.GEMINI_API_KEY)

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def data_cache_dir(self) -> Path:
        path = self.BASE_DIR / "data_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        path = self.BASE_DIR / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def vault_file_path(self) -> Path:
        return self.BASE_DIR / ".vault"

    @property
    def audit_dir(self) -> Path:
        path = self.BASE_DIR / "logs" / "audit"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_dir(self) -> Path:
        """Alias for data_cache_dir — used by main.py."""
        return self.data_cache_dir

    # ── Lowercase Aliases (for main.py convenience) ──────

    @property
    def db_host(self) -> str:
        return self.DB_HOST

    @property
    def db_port(self) -> int:
        return self.DB_PORT

    @property
    def db_name(self) -> str:
        return self.DB_NAME

    @property
    def db_user(self) -> str:
        return self.DB_USER

    @property
    def db_password(self) -> str:
        return self.DB_PASSWORD

    @property
    def db_pool_min(self) -> int:
        return self.DB_MIN_CONNECTIONS

    @property
    def db_pool_max(self) -> int:
        return self.DB_MAX_CONNECTIONS

    @property
    def vault_dir(self) -> Path:
        """Alias for vault_file_path — used by main.py."""
        return self.vault_file_path

    @property
    def trading_mode(self) -> str:
        """Default trading mode — runtime-tunable via app_config."""
        return "PAPER"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


# ── Singleton ────────────────────────────────────────────────

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the singleton Settings instance.

    Raises:
        ConfigurationError: If .env file has invalid values or is unreadable.
    """
    global _settings
    if _settings is None:
        try:
            _settings = Settings()
        except Exception as e:
            raise ConfigurationError(
                f"Failed to load settings: {e}. "
                "Check your .env file for invalid values.",
                details={"original_error": type(e).__name__, "message": str(e)},
            )
    return _settings


def reset_settings() -> None:
    """Reset settings singleton (useful for testing)."""
    global _settings
    _settings = None
