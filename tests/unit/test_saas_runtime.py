"""Hosted channel isolation, compiler authority, verified identity and lifecycle regressions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import yaml

from gryphon.errors import (
    CapacityError,
    CompileError,
    ConfigurationError,
    ConflictError,
    InputValidationError,
    SecurityViolationError,
)
from gryphon.models import BasicAuthConfig, Channel, SaaSSpec, StaticAuthConfig, SwaggerSource
from gryphon.runtime.recovery_lease import RecoveryLease
from gryphon.saas_catalog import channel_config, compile_catalog, validate_selection, validate_uploaded_document
from gryphon.saas_runtime import ChannelRuntimeManager, verified_channel
from gryphon.security.auth import AsyncVault
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.http import StarletteWithLifespan

    from gryphon.config import GryphonConfig
    from gryphon.runtime.registry import Registry


def _spec(name: str, tenant_id: str) -> SaaSSpec:
    """Use the shared weather fixture as an immutable tenant-owned JSON upload."""
    document = yaml.safe_load((Path(__file__).parents[1] / "fixtures" / "weather_api.yaml").read_text())
    metadata = {"id": str(uuid4()), "tenant_id": tenant_id, "name": name, "created_at": 1}
    digest = hashlib.sha256(json.dumps(document).encode()).hexdigest()
    return SaaSSpec.model_validate({**metadata, "document": document, "sha256": digest})


def _channel(specs: list[SaaSSpec]) -> Channel:
    """Build a server-issued channel identity without process environment settings."""
    metadata = {"id": str(uuid4()), "tenant_id": specs[0].tenant_id, "name": "Example", "created_at": 1}
    return Channel.model_validate({**metadata, "spec_ids": [spec.id for spec in specs]})


def _binding() -> tuple[SaaSSpec, Channel]:
    """Create one standard tenant specification and its channel binding."""
    spec = _spec("weather", uuid4().hex)
    return spec, _channel([spec])


def _code(name: str = "weather") -> str:
    """Build a public fixture capability call without credential or owner arguments."""
    return f'result = await call_tool("{name}.get_current_weather", {{"city": "London"}})'


@pytest.fixture
async def manager(gryphon_config: GryphonConfig, tmp_path: Path) -> AsyncIterator[ChannelRuntimeManager]:
    """Always close hosted workers and their separate persistent stores."""
    config = gryphon_config.model_copy(update={"max_output_size_bytes": 1024})
    runtime = ChannelRuntimeManager(config, tmp_path / "hosted", 2)
    try:
        yield runtime
    finally:
        await runtime.close()


async def _request(
    app: StarletteWithLifespan,
    channel_id: str | None,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send real stateless MCP HTTP requests with independently verified context."""
    context = verified_channel.set(channel_id)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            headers = {
                "Authorization": "Bearer externally-verified",
                "Accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            }
            params = {"name": name, "arguments": arguments or {}}
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
            return await client.post("/", headers=headers, json=body)
    finally:
        verified_channel.reset(context)


async def _tool(
    app: StarletteWithLifespan,
    channel_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require the real SDK to return native structured tool results."""
    response = await _request(app, channel_id, name, arguments)
    assert response.status_code == 200, response.text
    data = response.json()["result"]["structuredContent"]
    assert isinstance(data, dict)
    return data


async def test_runtime_two_channels_discovery_and_execution_are_separate(manager: ChannelRuntimeManager) -> None:
    """Different tenant documents expose separate catalogs and real broker capabilities."""
    first, second = _spec("weather", uuid4().hex), _spec("climate", uuid4().hex)
    one, two = _channel([first]), _channel([second])
    async with manager.acquire(one, [first]) as app_one, manager.acquire(two, [second]) as app_two:
        for app, channel, name in ((app_one, one, "weather"), (app_two, two, "climate")):
            listing = await _tool(app, channel.id, "list_servers")
            assert [item["name"] for item in listing["servers"]] == [name]
            functions = [{"server_name": name, "function_name": "get_current_weather"}]
            inspected = await _tool(app, channel.id, "get_functions", {"functions": functions})
            assert inspected["functions"][0]["input_schema"]["required"] == ["city"]
            response = httpx.Response(200, json={"temperature": 21})
            with patch.object(NetworkClient, "request", new=AsyncMock(return_value=response)):
                arguments = {"code": _code(name), "description": "Read public weather"}
                result = await _tool(app, channel.id, "execute_code", arguments)
            assert result["success"] and result["data"]["temperature"] == 21
        arguments = {"code": _code(), "description": "Try foreign catalog"}
        foreign = await _tool(app_two, two.id, "execute_code", arguments)
        assert not foreign["success"]


async def test_runtime_handles_are_channel_owned(manager: ChannelRuntimeManager) -> None:
    """Recipes, receipts and artifacts cannot cross channels, including within one tenant."""
    spec = _spec("weather", uuid4().hex)
    one, two = _channel([spec]), _channel([spec])
    async with manager.acquire(one, [spec]) as app_one, manager.acquire(two, [spec]) as app_two:
        result = await _tool(
            app_one, one.id, "execute_code", {"code": 'result = "x" * 4000', "description": "Large data"}
        )
        assert result["success"] and result["artifact_id"] and result["cache_id"]
        for name, arguments, category in (
            ("run_cached_code", {"cache_id": result["cache_id"]}, "cache"),
            ("get_run", {"run_id": result["run_id"]}, "not_found"),
            ("read_artifact", {"artifact_id": result["artifact_id"]}, "cache"),
        ):
            denied = await _tool(app_two, two.id, name, arguments)
            assert denied["error_type"] == category
        recipes = await _tool(app_one, one.id, "list_recipes")
        assert recipes["recipes"][0]["id"] == result["cache_id"]
        own = await _tool(app_one, one.id, "get_run", {"run_id": result["run_id"]})
        assert own["status"] == "succeeded"


async def test_runtime_authentication_requires_context_not_headers(manager: ChannelRuntimeManager) -> None:
    """An arbitrary bearer or another authenticated channel cannot become this channel's owner."""
    spec, channel = _binding()
    async with manager.acquire(channel, [spec]) as app:
        assert (await _request(app, None, "list_servers")).status_code == 401
        assert (await _request(app, uuid4().hex, "list_servers")).status_code == 401
        assert (await _request(app, channel.id, "list_servers")).status_code == 200


async def test_runtime_host_environment_credentials_never_reach_upstream(
    manager: ChannelRuntimeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant cannot claim an operator's GRYPHON server-name credentials or extra headers."""
    monkeypatch.setenv("GRYPHON_WEATHER_AUTH", "Bearer synthetic-host-only")
    monkeypatch.setenv("GRYPHON_WEATHER_COOKIE", "session=synthetic-host-only")
    monkeypatch.setenv("GRYPHON_WEATHER_EXTRA_HEADERS", '{"X-Operator": "synthetic-host-only"}')
    spec, channel = _binding()
    network = AsyncMock(return_value=httpx.Response(200, json={"temperature": 9}))
    async with manager.acquire(channel, [spec]) as app:
        with patch.object(NetworkClient, "request", new=network):
            result = await _tool(app, channel.id, "execute_code", {"code": _code(), "description": "Public call"})
        assert result["success"]
        assert not {"authorization", "cookie", "x-operator"} & {
            key.lower() for key in network.call_args.kwargs["headers"]
        }


async def test_runtime_invalidation_revokes_before_request_cleanup(manager: ChannelRuntimeManager) -> None:
    """Invalidation immediately denies old app authority while waiting for acquired references."""
    spec, channel = _binding()
    async with manager.acquire(channel, [spec]) as app:
        invalidating = asyncio.create_task(manager.invalidate(channel.id))
        await asyncio.sleep(0)
        assert not invalidating.done()
        assert (await _request(app, channel.id, "list_servers")).status_code == 401
    await invalidating
    with pytest.raises(ConflictError):
        async with manager.acquire(channel, [spec]):
            pytest.fail("Revoked revision was reacquired")
    channel.revision += 1
    async with manager.acquire(channel, [spec]) as replacement:
        assert replacement is not app
        assert (await _request(replacement, channel.id, "list_servers")).status_code == 200


async def test_runtime_capacity_does_not_evict_idle_app(manager: ChannelRuntimeManager) -> None:
    """Request completion does not authorize eviction because background jobs may still own work."""
    spec = _spec("weather", uuid4().hex)
    one, two, three = _channel([spec]), _channel([spec]), _channel([spec])
    async with manager.acquire(one, [spec]) as original:
        pass
    async with manager.acquire(two, [spec]):
        pass
    with pytest.raises(CapacityError):
        async with manager.acquire(three, [spec]):
            pytest.fail("Capacity should fail closed")
    async with manager.acquire(one, [spec]) as reused:
        assert original is reused


@pytest.mark.parametrize("revision", [1, 2, 3])
@pytest.mark.parametrize("initializing", [False, True])
async def test_runtime_rotation_preserves_new_revision_and_rejects_stale_initialization(
    manager: ChannelRuntimeManager,
    revision: int,
    initializing: bool,
) -> None:
    """A committed cutoff cannot tombstone fresh authority or allow stale startup to yield."""
    spec, channel = _binding()
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def acquire_once() -> StarletteWithLifespan:
        """Release the ordinary request reference before awaiting administrative cleanup."""
        async with manager.acquire(channel, [spec]) as app:
            return app

    async def delayed(*args: Any, **kwargs: Any) -> Registry:
        """Pause real compilation to deterministically race key rotation against startup."""
        entered.set()
        await proceed.wait()
        return await compile_catalog(*args, **kwargs)

    if not initializing:
        await acquire_once()
        proceed.set()
    channel.revision = revision
    with patch("gryphon.saas_runtime.compile_catalog", new=delayed):
        starting = asyncio.create_task(acquire_once())
        await entered.wait() if initializing else await asyncio.shield(starting)
        invalidating = asyncio.create_task(manager.invalidate(channel.id, before_revision=2))
        await asyncio.sleep(0)
        proceed.set()
        if initializing and revision == 1:
            with pytest.raises(ConflictError):
                await starting
        else:
            await starting
        await invalidating
    with pytest.raises(ConflictError):
        async with manager.acquire(channel.model_copy(update={"revision": 1}), [spec]):
            pytest.fail("Stale channel revision was accepted")
    channel.revision = max(revision, 2)
    async with manager.acquire(channel, [spec]) as app:
        assert (await _request(app, channel.id, "list_servers")).status_code == 200


@pytest.mark.parametrize("target", ["CacheStore.initialize", "CodeExecutor.startup"])
async def test_runtime_partial_initialization_closes_dependencies(
    manager: ChannelRuntimeManager,
    tmp_path: Path,
    target: str,
) -> None:
    """A failure after cache and broker creation closes them and leaves the run lease reusable."""
    spec, channel = _binding()
    with (
        patch(f"gryphon.saas_runtime.{target}", new=AsyncMock(side_effect=RuntimeError("synthetic failure"))),
        pytest.raises(RuntimeError, match="synthetic failure"),
    ):
        async with manager.acquire(channel, [spec]):
            pytest.fail("Partial startup should fail")
    lease = RecoveryLease(str(tmp_path / "hosted" / "channels" / channel.id / "runs.db"))
    lease.acquire()
    lease.close()
    async with manager.acquire(channel, [spec]) as app:
        assert (await _request(app, channel.id, "list_servers")).status_code == 200


async def test_runtime_close_is_idempotent_and_rejects_new_acquires(manager: ChannelRuntimeManager) -> None:
    """Shutdown closes idle runtimes and permanently denies future admission."""
    spec, channel = _binding()
    async with manager.acquire(channel, [spec]):
        pass
    await manager.close()
    await manager.close()
    with pytest.raises(SecurityViolationError):
        async with manager.acquire(channel, [spec]):
            pytest.fail("Closed manager admitted a request")


@pytest.mark.parametrize("reference", ["file:///etc/passwd", "../../operator.json", "https://example.com/spec", 4])
def test_catalog_external_references_are_rejected(reference: Any) -> None:
    """Unused external references are denied too, not merely references reached by compilation."""
    with pytest.raises(CompileError):
        validate_uploaded_document({"unused": {"$ref": reference}}, 1024)


def test_catalog_interpolation_and_bad_json_are_rejected() -> None:
    """No JSON field can resolve host environment variables or non-finite values."""
    for document in ({"title": "${GRYPHON_OPERATOR_AUTH}"}, {"n": float("nan")}, {"title": "x" * 2048}):
        with pytest.raises(CompileError):
            validate_uploaded_document(document, 1024)


def test_catalog_unvalidated_identifiers_and_foreign_specs_are_rejected() -> None:
    """Validate namespace IDs before deriving any runtime paths."""
    spec, channel = _binding()
    with pytest.raises(InputValidationError):
        validate_selection(channel.model_copy(update={"id": "../../escape"}), [spec])
    with pytest.raises(SecurityViolationError):
        validate_selection(channel, [spec.model_copy(update={"tenant_id": uuid4().hex})])
    with pytest.raises(InputValidationError):
        validate_selection(channel, [])
    with pytest.raises(SecurityViolationError):
        validate_selection(channel.model_copy(update={"enabled": False}), [spec])


@pytest.mark.parametrize("inline", [False, True])
async def test_catalog_bound_upload_overrides_host_sources_and_retains_stable_identity(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    inline: bool,
) -> None:
    """Inline host sources, including an empty override, cannot replace uploaded channel bindings."""
    spec, channel = _binding()
    source = SwaggerSource(name="foreign", swagger_url="unbound.json", auth=StaticAuthConfig(value="${HOST_AUTH}"))
    base = gryphon_config.model_copy(update={"swaggers": [source] if inline else []})
    isolated = channel_config(base, channel, tmp_path)
    first = await compile_catalog(base, channel, [spec])
    second = await compile_catalog(isolated, channel, [spec])
    assert [server.name for server in first.list_servers()] == ["weather"]
    assert first.fingerprint() == second.fingerprint()
    assert isolated.swaggers is None and base.swaggers == ([source] if inline else [])
    assert not first._compiled_dir.exists()


async def test_hosted_vault_explicit_credentials_reject_interpolation(gryphon_config: GryphonConfig) -> None:
    """Environment expansion is forbidden even when future operator code supplies explicit auth."""
    network = NetworkClient(gryphon_config)
    vault = AsyncVault(network, allow_environment=False)
    try:
        for auth in (StaticAuthConfig(value="${GRYPHON_AUTH}"), BasicAuthConfig(username="a", password="${PASSWORD}")):
            with pytest.raises(ConfigurationError):
                await vault.resolve("weather", auth)
        assert await vault.resolve("weather", StaticAuthConfig(value="Bearer explicit-test")) == {
            "Authorization": "Bearer explicit-test",
        }
    finally:
        vault.close()
        await network.close()


def test_catalog_config_copies_do_not_reload_environment(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel copies retain operator limits rather than reconsulting the process environment."""
    monkeypatch.setenv("GRYPHON_MAX_TOOL_CALLS", "999")
    monkeypatch.setenv("GRYPHON_SWAGGERS", "invalid-environment-source-config")
    spec, channel = _binding()
    config = channel_config(gryphon_config, channel, tmp_path)
    assert config.max_tool_calls == gryphon_config.max_tool_calls
    assert config.cache_db_path != gryphon_config.cache_db_path
    assert not config.allow_writes and config.http_auth_token is None and config.swaggers is None


async def test_runtime_invalidation_cancels_background_job(manager: ChannelRuntimeManager) -> None:
    """Revocation cancels accepted background work and persists its terminal receipt."""
    spec, channel = _binding()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> httpx.Response:
        """Keep a synthetic upstream request pending until executor cancellation reaches it."""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return httpx.Response(200, json={})

    with patch.object(NetworkClient, "request", new=blocked):
        async with manager.acquire(channel, [spec]) as app:
            code = 'result = await call_tool("weather.get_current_weather", {"city": "London"})'
            receipt = await _tool(app, channel.id, "submit_code", {"code": code, "description": "Pending call"})
        async with asyncio.timeout(5):
            await started.wait()
            await manager.invalidate(channel.id)
        assert stopped.is_set()
    channel.revision += 1
    async with manager.acquire(channel, [spec]) as replacement:
        record = await _tool(replacement, channel.id, "get_run", {"run_id": receipt["id"]})
        assert record["status"] == "cancelled"
