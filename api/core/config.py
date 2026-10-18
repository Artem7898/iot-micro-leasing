from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    IOT_STATE_DIR: Path = BASE_DIR / ".local" / "state"
    DATABASE_URL: str = ""
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    INVOICE_DIR: Path | None = None
    API_KEY: SecretStr | None = None
    API_AUTH_ENABLED: bool = True
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT: str = "500/minute"
    MAX_REQUEST_BYTES: int = Field(default=16_384, ge=1024, le=1_048_576)
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=32)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0, le=32)
    DB_POOL_TIMEOUT: int = Field(default=10, ge=1, le=60)
    DB_CONNECT_TIMEOUT: int = Field(default=5, ge=1, le=30)
    AUTO_CREATE_SCHEMA: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("REDIS_URL")
    @classmethod
    def redis_scheme(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        return value

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        self.IOT_STATE_DIR = self.IOT_STATE_DIR.expanduser().resolve()
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite:///{self.IOT_STATE_DIR / 'dev.sqlite3'}"
        if self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            raise ValueError("Use the synchronous postgresql+psycopg:// database driver")
        if not self.DATABASE_URL.startswith(
            ("sqlite://", "postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("DATABASE_URL must use SQLite or synchronous PostgreSQL")
        if self.DATABASE_URL.startswith("postgresql://"):
            self.DATABASE_URL = self.DATABASE_URL.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        self.INVOICE_DIR = (
            (self.INVOICE_DIR or self.IOT_STATE_DIR / "invoices").expanduser().resolve()
        )
        if self.ENVIRONMENT == "production":
            if (
                not self.API_AUTH_ENABLED
                or not self.API_KEY
                or len(self.API_KEY.get_secret_value()) < 32
            ):
                raise ValueError(
                    "Production requires API authentication and an API_KEY of at least 32 characters"
                )
            if not self.DATABASE_URL.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL")
            if self.AUTO_CREATE_SCHEMA:
                raise ValueError("Use migrations instead of AUTO_CREATE_SCHEMA in production")
        return self


settings = Settings()
