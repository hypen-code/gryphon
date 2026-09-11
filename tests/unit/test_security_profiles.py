"""Offline-profile, DNS-pinning and source-bound regression tests."""

from __future__ import annotations

import ast
import asyncio
import inspect
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from gryphon.errors import ConfigurationError, ExecutionError, SecurityViolationError
from gryphon.models import OAuth2AuthConfig
from gryphon.security import ast_guard, auth, broker, encoding, network, policies, response, schema, vault
from gryphon.security.ast_guard import ASTGuard
from gryphon.security.auth import AsyncVault
from gryphon.security.network import NetworkClient, decode_json

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


@pytest.mark.parametrize("source", ["import numpy as np", "import pandas as pd", "from numpy import mean"])
def test_numeric_imports_require_explicit_offline_grant(source: str) -> None:
    guard = ASTGuard()
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate(source)
    guard.validate(source, additional_allowed_modules=frozenset({"numpy", "pandas"}))
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        guard.validate(source)


@pytest.mark.parametrize(
    "source", ["import numpy.linalg", "from pandas import _libs", "from weather.functions import lookup"]
)
def test_numeric_grant_does_not_allow_arbitrary_imports(source: str) -> None:
    with pytest.raises(SecurityViolationError, match="blocked_import"):
        ASTGuard().validate(source, additional_allowed_modules=frozenset({"numpy", "pandas"}))


@pytest.mark.parametrize("modules", [frozenset({"os"}), frozenset({"httpx"}), frozenset({"weather.functions"})])
def test_import_profile_cannot_override_blocked_or_unknown_modules(modules: frozenset[str]) -> None:
    with pytest.raises(SecurityViolationError, match="Invalid offline import profile"):
        ASTGuard().validate("result = 1", additional_allowed_modules=modules)


async def test_ipv4_is_preferred_after_every_address_is_validated(gryphon_config: GryphonConfig) -> None:
    requests: list[httpx.Request] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Return IPv6 first to exercise explicit IPv4 preference."""
        return ["2001:4860:4860::8888", "93.184.216.34"]

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture the destination actually selected by the transport."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(gryphon_config, resolver=resolve, transport=httpx.MockTransport(handle))
    try:
        await client.request("GET", "https://api.example.com")
        assert requests[0].url.host == "93.184.216.34"
    finally:
        await client.close()


async def test_invalid_ipv6_answer_blocks_even_when_public_ipv4_exists(gryphon_config: GryphonConfig) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        """Provide an unsafe answer alongside an otherwise usable public address."""
        return ["::1", "93.184.216.34"]

    client = NetworkClient(gryphon_config, resolver=resolve)
    try:
        with pytest.raises(SecurityViolationError):
            await client.request("GET", "https://api.example.com")
    finally:
        await client.close()


async def test_real_transport_factory_is_verified_and_origin_isolated(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings: list[dict[str, Any]] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Make different original hosts share a numeric address."""
        return ["93.184.216.34"]

    def factory(**kwargs: Any) -> httpx.MockTransport:
        """Replace only the actual connection factory, not validation or client logic."""
        settings.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    client = NetworkClient(gryphon_config, resolver=resolve)
    try:
        await client.request("GET", "https://first.example.com")
        await client.request("GET", "https://second.example.com")
        assert len(settings) == 2
        assert all(item["verify"] is True and item["trust_env"] is False and item["retries"] == 0 for item in settings)
    finally:
        await client.close()


async def test_closing_vault_during_login_prevents_cache_repopulation(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = NetworkClient(gryphon_config)
    resolver = AsyncVault(client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def login(config: OAuth2AuthConfig) -> tuple[dict[str, str], float]:
        """Pause a mocked token fetch until authority has been revoked."""
        entered.set()
        await release.wait()
        return {"Authorization": "Bearer mock-token"}, 3600.0

    monkeypatch.setattr(resolver, "_oauth", AsyncMock(side_effect=login))
    task = asyncio.create_task(
        resolver.resolve(
            "svc",
            OAuth2AuthConfig(
                token_url="https://auth.example.com",
                client_id="test",
                client_secret="test",
            ),
        )
    )
    try:
        await entered.wait()
        resolver.close()
        release.set()
        with pytest.raises(ConfigurationError, match="closed"):
            await task
        assert not resolver._cache
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("content", [b"1e400", b"-1e400", b'{"value":NaN}'])
def test_nonfinite_response_numbers_rejected(content: bytes) -> None:
    with pytest.raises(ExecutionError, match="^Upstream returned invalid JSON$"):
        decode_json(httpx.Response(200, content=content))


def test_security_sources_meet_size_and_function_bounds() -> None:
    for module in (ast_guard, auth, broker, encoding, network, policies, response, schema, vault):
        source = inspect.getsource(module)
        assert len(source.splitlines()) <= 400, module.__name__
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.end_lineno is not None
                assert node.end_lineno - node.lineno + 1 <= 50, (module.__name__, node.name)
