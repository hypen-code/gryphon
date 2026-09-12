"""Pydantic settings for Gryphon configuration loaded from environment variables."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from gryphon.models import ReadOnlyPostOperation, SwaggerSource
from gryphon.security.ucp_identity import validate_profile

# Resolve .env with a fallback chain:
#   1. CWD/.env      — works when the server is launched from the project root
#   2. <repo-root>/.env — works when the MCP client sets a different working directory
#      (config.py lives at src/gryphon/config.py, so three parents up = project root)
_CWD_ENV = Path.cwd() / ".env"
_PKG_ENV = Path(__file__).parent.parent.parent / ".env"
_ENV_FILE = _PKG_ENV if not _CWD_ENV.exists() and (_PKG_ENV.parent / "pyproject.toml").is_file() else _CWD_ENV


class GryphonConfig(BaseSettings):
    """Main Gryphon server configuration loaded from GRYPHON_ prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="GRYPHON_",
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    debug: bool = False
    http_auth_token: SecretStr | None = None
    context_budget_bytes: int = Field(default=16384, ge=1024, le=262144)
    discovery_limit: int = Field(default=10, ge=1, le=100)
    include_function_summaries: bool = False

    # Compiler
    compile_on_startup: bool = True
    compiled_output_dir: str = "./compiled"
    swagger_config_file: str = "./config/swaggers.yaml"
    swaggers: Annotated[list[SwaggerSource] | None, NoDecode] = Field(default=None, repr=False, validate_default=False)
    state_dir: str | None = None
    # LiteLLM model string — use provider/model format, e.g.:
    #   openai/gpt-4o  |  anthropic/claude-3-5-sonnet-20241022
    #   gemini/gemini-2.0-flash  |  openrouter/mistralai/mistral-7b-instruct
    llm_enhance: bool = False
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = "gemini/gemini-2.0-flash"

    # Executor
    lint_enabled: bool = False  # The restricted VM validates syntax independently of optional lint.
    sandbox_requirements_path: str = "./sandbox/requirements.txt"
    docker_image: str = "gryphon-sandbox:2.0.0"
    docker_host: str = ""  # e.g. unix:///home/user/.docker/desktop/docker.sock
    execution_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_output_size_bytes: int = Field(default=65536, ge=1024, le=1048576)  # Bounded inline output
    network_mode: Literal["none"] = "none"
    # Restricted Python is the default; Docker is an explicit offline compute profile.
    sandbox_mode: Literal["restricted", "docker"] = "restricted"
    sandbox_allowed_imports: list[str] | None = None
    # Admission is bounded; completed execution environments are never reused.
    max_concurrent_executions: int = Field(default=4, ge=1, le=64)
    queue_timeout_seconds: int = Field(default=5, ge=1, le=60)
    max_tool_calls: int = Field(default=50, ge=1, le=1000)
    sandbox_memory_bytes: int = Field(default=64000000, ge=1000000, le=512000000)
    docker_runtime: str = "runsc"
    artifact_dir: str = "./data/artifacts"
    artifact_max_entries: int = Field(default=100, ge=1, le=10000)
    run_db_path: str = "./data/runs.db"
    run_ttl_seconds: int = Field(default=86400, ge=60)
    run_max_entries: int = Field(default=1000, ge=1)

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=3600, ge=1)
    cache_max_entries: int = Field(default=500, ge=1)
    cache_db_path: str = "./data/cache.db"

    # Security
    allowed_domains: list[str] = Field(default_factory=list)
    ucp_agent_profile: str | None = Field(default=None, repr=False)
    max_code_size_bytes: int = Field(default=65536, ge=1, le=262144)  # 64KB default
    allow_private_networks: bool = False
    allow_writes: bool = False
    allow_catalog_posts: bool = False
    allowed_write_operations: list[str] = Field(default_factory=list)
    allowed_read_only_post_operations: list[ReadOnlyPostOperation] = Field(default_factory=list, max_length=1000)
    http_timeout_seconds: int = Field(default=15, ge=1, le=60)
    max_response_size_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    max_spec_size_bytes: int = Field(default=5242880, ge=1024, le=16777216)

    # Docker memory limit for sandbox containers — increase for ML workloads (e.g. Prophet, scikit-learn)
    container_memory_limit: str = "256m"

    # Optional tools — disabled by default; set GRYPHON_ENABLE_ADDITIONAL_TOOLS=true to enable
    enable_additional_tools: bool = False

    @model_validator(mode="after")
    def _validate_ucp_agent_profile(self) -> GryphonConfig:
        """Require an explicit public HTTPS profile under the operator domain policy."""
        if self.ucp_agent_profile is not None:
            validate_profile(self.ucp_agent_profile, self.allowed_domains)
        return self

    @field_validator("swaggers", mode="before")
    @classmethod
    def _validate_swagger_sources(cls, value: object) -> list[SwaggerSource] | None:
        """Require an environment JSON list while retaining the programmatic YAML fallback sentinel."""
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list):
            raise ValueError("GRYPHON_SWAGGERS must be a JSON list")
        return [SwaggerSource.model_validate(entry) for entry in value]

    @field_validator("http_auth_token")
    @classmethod
    def _validate_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Require sufficient entropy space for configured bearer tokens."""
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("HTTP authentication tokens must contain at least 32 characters")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def _normalize_domains(cls, values: list[str]) -> list[str]:
        """Accept explicit hostnames rather than wildcard or URL policy entries."""
        normalized = sorted({value.lower().rstrip(".") for value in values})
        if any(not value or any(char in value for char in "/:@* ") for value in normalized):
            raise ValueError("Allowed domains must be explicit hostnames")
        return normalized


def load_config(env_file: str | None = None, *, discover_env: bool = True) -> GryphonConfig:
    """Load and return the Gryphon configuration.

    Args:
        env_file: Optional path to a custom .env file. Overrides the default CWD/.env.
        discover_env: Whether to read the default env file when no explicit path is supplied.

    Returns:
        Populated GryphonConfig instance.
    """
    if env_file is not None or not discover_env:
        return GryphonConfig(_env_file=env_file)  # type: ignore[call-arg]
    return GryphonConfig()
