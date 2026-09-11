"""Regression tests for bounded output contracts and host-only credential handling."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gryphon.errors import (
    CapacityError,
    ConfigurationError,
    ExecutionError,
    ExecutionTimeoutError,
    SecurityViolationError,
)
from gryphon.models import EndpointManifest, ExecutionScope, ServerManifest, StaticAuthConfig
from gryphon.runtime.registry import Registry
from gryphon.security.auth import AsyncVault
from gryphon.security.broker import ToolBroker
from gryphon.security.network import NetworkClient
from gryphon.security.response import validate_response
from gryphon.security.schema import validate_contract

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


def _manifest() -> ServerManifest:
    """Build a complete in-memory capability contract."""
    endpoint = EndpointManifest(
        function_name="read",
        summary="",
        method="GET",
        path="/items",
        parameters_summary="",
        response_summary="",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    return ServerManifest(
        server_name="svc",
        description="",
        swagger_hash="test",
        compiled_at="now",
        is_read_only=True,
        base_url="https://api.example.com",
        endpoints=[endpoint],
    )


@pytest.fixture
async def response_broker(
    gryphon_config: GryphonConfig,
) -> AsyncIterator[tuple[ToolBroker, EndpointManifest, dict[str, Any]]]:
    """Create a credential-bearing broker with injected HTTP and DNS only."""
    manifest = _manifest()
    endpoint = manifest.endpoints[0]
    registry = MagicMock(spec=Registry)
    registry.get_manifest.return_value, registry.get_endpoint.return_value = manifest, endpoint
    state: dict[str, Any] = {"payload": {"ok": True}, "requests": []}

    async def resolver(host: str, port: int) -> list[str]:
        """Use a fixed public numeric address without DNS."""
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        """Return controlled JSON and record exactly what reached the transport."""
        state["requests"].append(request)
        return httpx.Response(200, json=state["payload"])

    broker = ToolBroker(gryphon_config, registry, {"svc": StaticAuthConfig(value="Bearer mock-sensitive-token")})
    await broker._network.close()
    broker._network = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    broker._vault = AsyncVault(broker._network)
    try:
        yield broker, endpoint, state
    finally:
        await broker.close()


def _scope() -> ExecutionScope:
    """Return fresh host authority with a monotonic deadline."""
    return ExecutionScope(run_id="test", deadline=time.monotonic() + 30)


async def test_broker_enforces_output_schema(response_broker: Any) -> None:
    broker, endpoint, state = response_broker
    endpoint.output_schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
    state["payload"] = {"count": "private upstream body"}
    with pytest.raises(ExecutionError, match="^Upstream response violated its declared schema$"):
        await broker.invoke("svc", "read", {}, _scope())


async def test_valid_output_schema_preserves_json(response_broker: Any) -> None:
    broker, endpoint, state = response_broker
    endpoint.output_schema = {"type": "array", "items": {"type": "number"}}
    state["payload"] = [1, 2.5]
    assert await broker.invoke("svc", "read", {}, _scope()) == [1, 2.5]


@pytest.mark.parametrize(
    "payload",
    [
        {"authorization": "Bearer mock-sensitive-token"},
        {"nested": [{"message": "debug: mock-sensitive-token"}]},
        {"mock-sensitive-token": "as a key"},
    ],
)
async def test_broker_rejects_credential_echo(response_broker: Any, payload: Any) -> None:
    broker, _, state = response_broker
    state["payload"] = payload
    with pytest.raises(ExecutionError, match="^Upstream response contained credential material$"):
        await broker.invoke("svc", "read", {}, _scope())


async def test_benign_sensitive_named_fields_preserved(response_broker: Any) -> None:
    broker, _, state = response_broker
    state["payload"] = {"password": "public documentation", "token": "unrelated value", "Authorization": "metadata"}
    assert await broker.invoke("svc", "read", {}, _scope()) == state["payload"]


@pytest.mark.parametrize(
    "headers, payload",
    [
        ({"Cookie": "sid=cookie-sensitive"}, {"cookie": "sid=cookie-sensitive"}),
        ({"Cookie": "sid=cookie-sensitive"}, {"value": "cookie-sensitive"}),
        ({"X-Api-Key": "private-api-key"}, ["echo private-api-key"]),
        ({"Authorization": "Basic dTpwYXNzd29yZA=="}, {"password": "password"}),
    ],
)
def test_known_credential_variants_fail_closed(headers: dict[str, str], payload: Any) -> None:
    with pytest.raises(ExecutionError, match="credential material"):
        validate_response(payload, {}, headers)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://forbidden.example.com/schema"},
        {"pattern": "^(a+)+$"},
        {"patternProperties": {".*": {"type": "string"}}},
        {"allOf": [{"type": "object"}]},
        {"anyOf": [{"type": "object"}]},
        {"oneOf": [{"type": "object"}]},
        {"uniqueItems": True},
        {"not": {"type": "null"}},
    ],
)
def test_input_and_output_share_bounded_schema_policy(schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_contract({}, schema)
    with pytest.raises(ExecutionError, match="^Upstream response violated its declared schema$"):
        validate_response({}, schema, {})


def test_bounded_schema_preserves_property_named_pattern() -> None:
    schema = {"type": "object", "properties": {"pattern": {"type": "string"}, "$ref": {"type": "string"}}}
    assert validate_response({"pattern": "ordinary data", "$ref": "ordinary data"}, schema, {})


def test_deep_response_structure_fails_closed() -> None:
    value: Any = "data"
    for _ in range(65):
        value = [value]
    with pytest.raises(ExecutionError, match="declared schema"):
        validate_response(value, {}, {})


def test_large_validation_work_is_bounded() -> None:
    schema = {
        "type": "array",
        "items": {"type": "object", "properties": {f"x{i}": {"type": "integer"} for i in range(500)}},
    }
    with pytest.raises(ExecutionError, match="declared schema"):
        validate_response([{}] * 2000, schema, {})


async def test_config_call_ceiling_cannot_be_raised_by_scope(response_broker: Any) -> None:
    broker, _, state = response_broker
    broker._config.max_tool_calls = 1
    scope = _scope()
    await broker.invoke("svc", "read", {}, scope)
    with pytest.raises(CapacityError):
        await broker.invoke("svc", "read", {}, scope)
    assert len(state["requests"]) == 1


async def test_revocation_during_auth_prevents_upstream_call(response_broker: Any) -> None:
    broker, _, state = response_broker
    scope = _scope()

    async def revoke(server: str, auth: Any) -> dict[str, str]:
        """Emulate host cancellation while resolving credentials."""
        scope.cancelled = True
        return {"Authorization": "Bearer mock-sensitive-token"}

    broker._vault.resolve = AsyncMock(side_effect=revoke)
    with pytest.raises(SecurityViolationError, match="revoked"):
        await broker.invoke("svc", "read", {}, scope)
    assert state["requests"] == []


async def test_deadline_includes_auth_wait(response_broker: Any) -> None:
    broker, _, state = response_broker
    scope = _scope()
    scope.deadline = time.monotonic() + 0.01

    async def wait(server: str, auth: Any) -> dict[str, str]:
        """Wait indefinitely without network I/O."""
        await asyncio.Event().wait()
        return {}

    broker._vault.resolve = AsyncMock(side_effect=wait)
    with pytest.raises(ExecutionTimeoutError):
        await broker.invoke("svc", "read", {}, scope)
    assert state["requests"] == []


async def test_typed_auth_retains_admin_headers(response_broker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    broker, _, state = response_broker
    monkeypatch.setenv("GRYPHON_SVC_EXTRA_HEADERS", json.dumps({"X-Tenant": "admin-tenant"}))
    await broker.invoke("svc", "read", {}, _scope())
    assert state["requests"][0].headers["X-Tenant"] == "admin-tenant"


async def test_vault_close_revokes_resolution(response_broker: Any) -> None:
    broker, _, _ = response_broker
    broker._vault.close()
    with pytest.raises(ConfigurationError, match="closed"):
        await broker._vault.resolve("svc", None)
