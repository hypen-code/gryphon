"""Capability broker tests with manifest doubles and fully mocked upstream I/O."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest

from gryphon.errors import (
    CapacityError,
    ExecutionError,
    ExecutionTimeoutError,
    InputValidationError,
    SecurityViolationError,
)
from gryphon.models import EndpointManifest, ExecutionScope, ParamSchema, ServerManifest, StaticAuthConfig
from gryphon.runtime.registry import Registry
from gryphon.security.auth import AsyncVault
from gryphon.security.broker import ToolBroker
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


def _endpoint() -> EndpointManifest:
    """Construct an authoritative endpoint without filesystem fixtures."""
    return EndpointManifest(
        function_name="lookup",
        summary="Lookup",
        method="GET",
        path="/items/{item}",
        parameters_summary="",
        response_summary="",
        base_url="https://api.example.com/v1",
        parameters=[ParamSchema(name="item", location="path", param_type="string", required=True)],
        input_schema={
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"],
            "additionalProperties": False,
        },
    )


@pytest.fixture
async def broker_setup(
    gryphon_config: GryphonConfig,
) -> AsyncIterator[tuple[ToolBroker, EndpointManifest, ServerManifest, list[httpx.Request], dict[str, Any]]]:
    """Create an in-memory manifest, scoped broker and deterministic network."""
    endpoint = _endpoint()
    manifest = ServerManifest(
        server_name="svc",
        description="API",
        swagger_hash="test",
        compiled_at="now",
        base_url=endpoint.base_url,
        is_read_only=False,
        endpoints=[endpoint],
    )
    registry = MagicMock(spec=Registry)
    registry.get_manifest.return_value = manifest
    registry.get_endpoint.return_value = endpoint
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {"status": 200, "content": b'{"ok":true}', "headers": {}}

    async def resolver(host: str, port: int) -> list[str]:
        """Supply a numeric public address without DNS."""
        return ["93.184.216.34"]

    def handle(request: httpx.Request) -> httpx.Response:
        """Return configurable response bytes and record actual transport requests."""
        requests.append(request)
        return httpx.Response(state["status"], content=state["content"], headers=state["headers"])

    broker = ToolBroker(gryphon_config, registry)
    await broker._network.close()
    broker._network = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handle))
    broker._vault = AsyncVault(broker._network)
    try:
        yield broker, endpoint, manifest, requests, state
    finally:
        await broker.close()


@pytest.fixture
def scope() -> ExecutionScope:
    """Create host-owned authority with a fresh monotonic deadline."""
    return ExecutionScope(run_id="test", deadline=time.monotonic() + 30)


async def test_broker_pins_host_and_sni(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, requests, _ = broker_setup
    assert await broker.invoke("svc", "lookup", {"item": "abc xyz"}, scope) == {"ok": True}
    request = requests[0]
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "api.example.com"
    assert request.extensions["sni_hostname"] == "api.example.com"
    assert request.url.raw_path == b"/v1/items/abc%20xyz"
    assert scope.calls == 1


@pytest.mark.parametrize("arguments", [{}, {"item": 1}, {"item": "x", "url": "https://other.example.com"}])
async def test_broker_rejects_schema_mismatch(
    broker_setup: Any, scope: ExecutionScope, arguments: dict[str, Any]
) -> None:
    broker, _, _, requests, _ = broker_setup
    with pytest.raises(InputValidationError):
        await broker.invoke("svc", "lookup", arguments, scope)
    assert requests == []


@pytest.mark.parametrize("value", ["..", "a/b", "a%2fb", "a?b", "a#b", "a\\b"])
async def test_broker_rejects_ambiguous_path_value(broker_setup: Any, scope: ExecutionScope, value: str) -> None:
    broker, _, _, requests, _ = broker_setup
    with pytest.raises(InputValidationError):
        await broker.invoke("svc", "lookup", {"item": value}, scope)
    assert not requests


@pytest.mark.parametrize("enabled, grants", [(False, ["svc.lookup"]), (True, []), (True, ["svc.*"])])
async def test_writes_require_both_admin_grants(
    broker_setup: Any,
    scope: ExecutionScope,
    enabled: bool,
    grants: list[str],
) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    endpoint.method = "POST"
    broker._config.allow_writes = enabled
    broker._config.allowed_write_operations = grants
    with pytest.raises(SecurityViolationError, match="administrator"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert not requests


async def test_read_only_source_overrides_write_grant(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, endpoint, manifest, _, _ = broker_setup
    endpoint.method = "DELETE"
    manifest.is_read_only = True
    broker._config.allow_writes = True
    broker._config.allowed_write_operations = ["svc.lookup"]
    with pytest.raises(SecurityViolationError, match="read-only"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)


async def test_authorized_write_encodes_body_without_retry(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, endpoint, _, requests, state = broker_setup
    endpoint.method = "POST"
    endpoint.request_body_schema = {"type": "object", "properties": {"enabled": {"type": "boolean"}}}
    endpoint.input_schema["properties"]["json_body"] = endpoint.request_body_schema
    broker._config.allow_writes = True
    broker._config.allowed_write_operations = ["svc.lookup"]
    state["status"] = 503
    with pytest.raises(ExecutionError, match="HTTP 503"):
        await broker.invoke("svc", "lookup", {"item": "x", "json_body": {"enabled": True}}, scope)
    assert len(requests) == 1
    assert requests[0].content == b'{"enabled":true}'


async def test_typed_query_header_encoding(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    for name, location, kind in [
        ("active", "query", "boolean"),
        ("count", "query", "integer"),
        ("X-Page", "header", "integer"),
    ]:
        endpoint.parameters.append(ParamSchema(name=name, location=location, param_type=kind))
        endpoint.input_schema["properties"][name] = {"type": kind}
    await broker.invoke("svc", "lookup", {"item": "x", "active": False, "count": 2, "X-Page": 3}, scope)
    assert requests[0].url.params == httpx.QueryParams({"active": "false", "count": "2"})
    assert requests[0].headers["X-Page"] == "3"


@pytest.mark.parametrize(
    "header", ["Authorization", "Host", "Cookie", "X-Api-Key", "X-Forwarded-Host", "Content-Length"]
)
async def test_declared_auth_and_routing_headers_cannot_override(
    broker_setup: Any, scope: ExecutionScope, header: str
) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    endpoint.parameters.append(ParamSchema(name=header, location="header", param_type="string"))
    endpoint.input_schema["properties"][header] = {"type": "string"}
    with pytest.raises(SecurityViolationError):
        await broker.invoke("svc", "lookup", {"item": "x", header: "override"}, scope)
    assert not requests


async def test_schema_remote_reference_never_fetches(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    endpoint.input_schema["properties"]["item"] = {"$ref": "https://other.example.com/schema"}
    with pytest.raises(InputValidationError, match="resolved"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert not requests


async def test_credentials_remain_host_only(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, requests, _ = broker_setup
    broker._auth_configs["svc"] = StaticAuthConfig(value="Bearer mock-token")
    result = await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert requests[0].headers["Authorization"] == "Bearer mock-token"
    assert "mock-token" not in str(result)


async def test_credentials_not_forwarded_to_endpoint_override(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    endpoint.base_url = "https://other.example.com"
    broker._auth_configs["svc"] = StaticAuthConfig(value="Bearer mock-token")
    with pytest.raises(SecurityViolationError, match="different upstream origin"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert not requests


async def test_call_budget_atomic_across_concurrent_invocations(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, requests, _ = broker_setup
    scope.max_calls = 1
    results = await asyncio.gather(
        broker.invoke("svc", "lookup", {"item": "x"}, scope),
        broker.invoke("svc", "lookup", {"item": "x"}, scope),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CapacityError) for result in results) == 1
    assert len(requests) == 1


async def test_scope_revocation_blocks_calls(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, requests, _ = broker_setup
    scope.cancelled = True
    with pytest.raises(SecurityViolationError, match="revoked"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert not requests


async def test_expired_scope_blocks_calls(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, requests, _ = broker_setup
    scope.deadline = time.monotonic() - 1
    with pytest.raises(ExecutionTimeoutError, match="deadline"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)
    assert not requests


@pytest.mark.parametrize("body", [b"private invalid text", b"NaN", b'{"x":Infinity}'])
async def test_invalid_upstream_json_sanitized(broker_setup: Any, scope: ExecutionScope, body: bytes) -> None:
    broker, _, _, _, state = broker_setup
    state["content"] = body
    with pytest.raises(ExecutionError, match="^Upstream returned invalid JSON$"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)


async def test_response_oversize_is_hard_error(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, _, state = broker_setup
    broker._config.max_response_size_bytes = 1024
    state["content"] = b"x" * 1025
    with pytest.raises(ExecutionError, match="size limit"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)


async def test_close_revokes_future_calls(broker_setup: Any, scope: ExecutionScope) -> None:
    broker, _, _, _, _ = broker_setup
    await broker.close()
    with pytest.raises(SecurityViolationError, match="revoked"):
        await broker.invoke("svc", "lookup", {"item": "x"}, scope)


async def test_admin_header_collision_is_case_insensitive(
    broker_setup: Any,
    scope: ExecutionScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, endpoint, _, requests, _ = broker_setup
    monkeypatch.setenv("GRYPHON_SVC_EXTRA_HEADERS", '{"X-Page": "admin"}')
    endpoint.parameters.append(ParamSchema(name="x-page", location="header", param_type="string"))
    endpoint.input_schema["properties"]["x-page"] = {"type": "string"}
    with pytest.raises(SecurityViolationError, match="header override"):
        await broker.invoke("svc", "lookup", {"item": "x", "x-page": "caller"}, scope)
    assert requests == []
