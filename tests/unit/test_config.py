"""Configuration defaults and fail-closed boundary validation for Gryphon."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from gryphon.config import GryphonConfig, load_config

if TYPE_CHECKING:
    from pathlib import Path


def _config(**values: Any) -> GryphonConfig:
    """Pass BaseSettings runtime controls without bypassing actual validation."""
    options: dict[str, Any] = {"_env_file": None, **values}
    return GryphonConfig(**options)


def test_defaults_select_restricted_networkless_execution() -> None:
    """Default settings do not grant arbitrary sandbox network access."""
    config = _config()
    assert (config.host, config.sandbox_mode, config.network_mode) == ("127.0.0.1", "restricted", "none")
    assert not config.allow_writes
    assert not config.allow_private_networks


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("port", 0),
        ("port", 65536),
        ("execution_timeout_seconds", 0),
        ("execution_timeout_seconds", 301),
        ("max_concurrent_executions", 0),
        ("max_tool_calls", 0),
        ("queue_timeout_seconds", 0),
        ("sandbox_memory_bytes", 0),
        ("max_output_size_bytes", 0),
        ("max_code_size_bytes", 0),
        ("cache_max_entries", 0),
        ("cache_ttl_seconds", 0),
        ("context_budget_bytes", 0),
        ("sandbox_mode", "warm"),
        ("network_mode", "host"),
        ("http_auth_token", "short-token"),
        ("allowed_domains", ["*.example.com"]),
        ("allowed_domains", ["https://example.com"]),
        ("allowed_domains", [""]),
    ],
)
def test_invalid_policy_setting_is_rejected(name: str, value: Any) -> None:
    """Invalid resource budgets and ambiguous network policy fail at startup."""
    with pytest.raises(ValidationError):
        _config(**{name: value})


def test_domains_are_normalized_and_deduplicated() -> None:
    """Policy uses deterministic, normalized hostname identities."""
    config = _config(allowed_domains=["API.Example.com.", "api.example.com"])
    assert config.allowed_domains == ["api.example.com"]


def test_config_defaults_are_not_shared_between_instances() -> None:
    """Mutable policy values cannot leak between configuration instances."""
    first = _config()
    second = _config()
    first.allowed_write_operations.append("weather.change")
    assert second.allowed_write_operations == []


def test_environment_uses_gryphon_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public configuration uses the rebranded environment namespace."""
    monkeypatch.setenv("GRYPHON_MAX_TOOL_CALLS", "7")
    assert load_config(env_file="/nonexistent/gryphon-test.env").max_tool_calls == 7


def test_http_token_is_not_exposed_by_repr() -> None:
    """Configured bearer credentials are secret values, not printable strings."""
    token = "synthetic-test-token-not-a-real-secret-000"
    config = _config(http_auth_token=token)
    assert token not in repr(config)


def test_shared_fixture_ignores_operator_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """The common test configuration never loads an operator's default dotenv file."""
    dotenv = tmp_path / "operator.env"
    dotenv.write_text("GRYPHON_MAX_TOOL_CALLS=1\n", encoding="utf-8")
    monkeypatch.setitem(GryphonConfig.model_config, "env_file", str(dotenv))
    monkeypatch.delenv("GRYPHON_MAX_TOOL_CALLS", raising=False)
    config = request.getfixturevalue("gryphon_config")
    assert config.max_tool_calls == 50
