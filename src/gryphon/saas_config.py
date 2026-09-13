"""Explicit operator configuration for the hosted control plane."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SaaSConfig(BaseSettings):
    """Require independent hosted credentials and a canonical browser origin."""

    model_config = SettingsConfigDict(env_prefix="GRYPHON_SAAS_", extra="ignore", env_file=None)

    admin_token: SecretStr
    database_url: SecretStr
    public_origin: str
    state_dir: Path = Path("./data/saas")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allow_insecure_http: bool = False
    docker_enabled: bool = False
    max_runtimes: int = Field(default=16, ge=1, le=128)
    max_http_requests: int = Field(default=32, ge=1, le=256)
    requests_per_minute: int = Field(default=600, ge=1, le=100000)
    login_attempts_per_minute: int = Field(default=20, ge=1, le=100)
    max_admin_sessions: int = Field(default=32, ge=1, le=256)
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    max_spec_bytes: int = Field(default=5242880, ge=1024, le=16777216)
    request_timeout_seconds: int = Field(default=15, ge=1, le=60)

    @field_validator("admin_token")
    @classmethod
    def _strong_token(cls, value: SecretStr) -> SecretStr:
        """Reject weak bootstrap credentials before opening any listener."""
        if len(value.get_secret_value()) < 32:
            raise ValueError("Administrator token must contain at least 32 characters")
        return value

    @field_validator("database_url")
    @classmethod
    def _database_scheme(cls, value: SecretStr) -> SecretStr:
        """Accept only the supported database backends without exposing the URL."""
        if not value.get_secret_value().startswith(("postgresql://", "postgres://", "sqlite:///")):
            raise ValueError("Unsupported control-plane database")
        return value

    @model_validator(mode="after")
    def _origin_policy(self) -> SaaSConfig:
        """Allow plaintext browser sessions only for explicitly selected loopback development."""
        try:
            url = urlsplit(self.public_origin)
            valid = url.hostname and url.port != 0 and not (url.username or url.password or url.query or url.fragment)
        except ValueError:
            raise ValueError("Invalid public origin") from None
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if not valid or url.path not in {"", "/"}:
            raise ValueError("Public origin must not contain paths or credentials")
        if url.scheme != "https" and not (url.scheme == "http" and self.allow_insecure_http and local):
            raise ValueError("Public origin requires HTTPS")
        self.public_origin = self.public_origin.rstrip("/")
        return self

    @property
    def secure_cookies(self) -> bool:
        """Use Secure cookies except in the explicit loopback-only development profile."""
        return self.public_origin.startswith("https://")
