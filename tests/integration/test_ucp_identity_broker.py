"""Compiled native UCP identities through the real broker and pinned mock transport."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_saas_ucp_mcp import ENDPOINT, object_schema

from gryphon.compiler.ucp_mcp import tools_to_openapi
from gryphon.errors import ExecutionError, SecurityViolationError, UpstreamDiagnosticError
from gryphon.models import Channel, ExecutionScope, SaaSSpec
from gryphon.saas_catalog import compile_catalog
from gryphon.security.broker import ToolBroker
from gryphon.security.mcp_client import discover_tools
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

PROFILE = "https://operator.example/agent.json"
CALLER = "https://caller.example/agent.json"


def tool() -> dict[str, Any]:
    """Reuse native object fixtures with the format-only required identity contract."""
    agent = object_schema({"profile": {"type": "string", "format": "uri"}})
    meta = object_schema({"ucp-agent": agent})
    meta["additionalProperties"] = True
    schema = object_schema({"id": {"type": "string"}, "meta": meta})
    return {"name": "get_cart", "inputSchema": schema}


class Peer:
    """Minimal isolated peer that cannot fetch agent profiles or inherit host auth."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.resolved: list[str] = []
        self.clients: list[NetworkClient] = []
        self.failure: str | None = None
        self.sse = False

    async def resolve(self, host: str, port: int) -> list[str]:
        """Resolve identities and merchant only to a synthetic public address."""
        assert host in {"operator.example", "caller.example", "merchant.myshopify.com"}
        assert port == 443
        self.resolved.append(host)
        return ["93.184.216.34"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Accept fixed-endpoint MCP only; never answer GET profile retrieval."""
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "merchant.myshopify.com"
        assert request.url.path == "/api/ucp/mcp"
        assert not {"authorization", "cookie", "x-api-key"} & set(request.headers)
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        assert request.method == "POST"
        body = json.loads(request.content)
        method = body["method"]
        if method == self.failure:
            return self.error_response(body["id"])
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "isolated", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [tool()]}
        else:
            assert method == "tools/call"
            result = {"structuredContent": {"cart": "ok"}, "content": []}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"Mcp-Session-Id": "synthetic-session"},
        )

    def error_response(self, response_id: str) -> httpx.Response:
        """Return an owned JSON/SSE RPC error without trusting its arbitrary text."""
        error = {
            "jsonrpc": "2.0",
            "id": response_id,
            "error": {
                "code": -32602,
                "message": "untrusted upstream message",
                "data": {"code": "invalid_profile_url"},
            },
        }
        if self.sse:
            return httpx.Response(
                200, content="data: " + json.dumps(error) + "\n\n", headers={"Content-Type": "text/event-stream"}
            )
        return httpx.Response(200, json=error)

    def factory(self, config: GryphonConfig) -> NetworkClient:
        """Preserve actual DNS/address/TLS policy and owned session cleanup."""
        client = NetworkClient(config, resolver=self.resolve, transport=httpx.MockTransport(self.handle))
        self.clients.append(client)
        return client


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch) -> Peer:
    """Patch only DNS and HTTP transport, never broker identity or protocol behavior."""
    value = Peer()
    for module in ("mcp_client", "broker"):
        monkeypatch.setattr("gryphon.security." + module + ".NetworkClient", value.factory)
    monkeypatch.setattr("gryphon.security.network.resolve_addresses", value.resolve)
    return value


async def _broker(config: GryphonConfig) -> ToolBroker:
    """Compile actual native metadata into the authoritative broker registry."""
    document, bindings = tools_to_openapi([tool()], ENDPOINT, "2026-08-25", None, 100000)
    spec = SaaSSpec(
        id="a" * 32,
        tenant_id="b" * 32,
        name="Shop",
        sha256="c" * 64,
        created_at=1.0,
        document=document,
        mcp_bindings=bindings,
        source_type="ucp_url",
        source_transport="mcp",
        source_url=ENDPOINT,
        resolved_endpoint=ENDPOINT,
    )
    channel = Channel(id="d" * 32, tenant_id=spec.tenant_id, name="Channel", spec_ids=[spec.id], created_at=1.0)
    registry = await compile_catalog(config, channel, [spec])
    return ToolBroker(config, registry, allow_environment=False)


@pytest.mark.parametrize("explicit", [None, PROFILE, CALLER])
async def test_native_identity_body_header_and_cleanup(
    gryphon_config: GryphonConfig,
    peer: Peer,
    explicit: str | None,
) -> None:
    config = gryphon_config.model_copy(update={"ucp_agent_profile": PROFILE, "allow_catalog_posts": True})
    broker = await _broker(config)
    arguments: dict[str, Any] = {"json_body": {"id": "cart", "meta": {"other": "preserved"}}}
    if explicit is not None:
        arguments["json_body"]["meta"]["ucp-agent"] = {"profile": explicit}
    original = deepcopy(arguments)
    try:
        scope = ExecutionScope(run_id="profile-test", deadline=time.monotonic() + 5)
        assert await broker.invoke("shop", "get_cart", arguments, scope) == {"cart": "ok"}
    finally:
        await broker.close()
    effective = explicit or PROFILE
    assert all(request.headers["ucp-agent"] == f'profile="{effective}"' for request in peer.requests)
    call = next(
        json.loads(request.content)
        for request in peer.requests
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/call"
    )
    assert call["params"]["arguments"]["meta"] == {"other": "preserved", "ucp-agent": {"profile": effective}}
    assert arguments == original
    assert peer.requests[-1].method == "DELETE"
    assert all(client._client.is_closed for client in peer.clients)
    assert ("caller.example" if explicit == CALLER else "operator.example") in peer.resolved


@pytest.mark.parametrize("profile", [None, "", "http://operator.example/p", 'https://operator.example/"'])
async def test_native_identity_invalid_fails_before_any_upstream(
    gryphon_config: GryphonConfig,
    peer: Peer,
    profile: str | None,
) -> None:
    config = gryphon_config.model_copy(
        update={"ucp_agent_profile": PROFILE if profile is not None else None, "allow_catalog_posts": True}
    )
    broker = await _broker(config)
    arguments: dict[str, Any] = {"json_body": {"id": "cart"}}
    if profile is not None:
        arguments["json_body"]["meta"] = {"ucp-agent": {"profile": profile}}
    try:
        with pytest.raises(ExecutionError, match="UCP agent profile"):
            await broker.invoke(
                "shop", "get_cart", arguments, ExecutionScope(run_id="invalid-profile", deadline=time.monotonic() + 5)
            )
    finally:
        await broker.close()
    assert peer.requests == [] and peer.resolved == []


async def test_native_profile_dns_rechecks_revocation_before_initialization(
    gryphon_config: GryphonConfig,
    peer: Peer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = gryphon_config.model_copy(update={"ucp_agent_profile": PROFILE, "allow_catalog_posts": True})
    broker = await _broker(config)
    scope = ExecutionScope(run_id="revoked-profile", deadline=time.monotonic() + 5)

    async def revoke(host: str, port: int) -> list[str]:
        scope.cancelled = True
        return await peer.resolve(host, port)

    monkeypatch.setattr("gryphon.security.network.resolve_addresses", revoke)
    try:
        with pytest.raises(SecurityViolationError, match="revoked"):
            await broker.invoke("shop", "get_cart", {"json_body": {"id": "cart"}}, scope)
    finally:
        await broker.close()
    assert peer.requests == []
    assert all(client._client.is_closed for client in peer.clients)


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("sse", [False, True])
async def test_native_profile_rpc_phase_matches_actual_method(
    gryphon_config: GryphonConfig,
    peer: Peer,
    method: str,
    sse: bool,
) -> None:
    peer.failure, peer.sse = method, sse
    config = gryphon_config.model_copy(update={"ucp_agent_profile": PROFILE, "allow_catalog_posts": True})
    broker = await _broker(config)
    try:
        with pytest.raises(UpstreamDiagnosticError) as caught:
            await broker.invoke(
                "shop",
                "get_cart",
                {"json_body": {"id": "cart"}},
                ExecutionScope(run_id="rpc-profile", deadline=time.monotonic() + 5),
            )
    finally:
        await broker.close()
    assert caught.value.diagnostic.phase == ("invoke" if method == "tools/call" else "discovery")
    assert caught.value.diagnostic.upstream_code == "invalid_profile_url"
    assert "untrusted upstream" not in str(caught.value)
    assert all(client._client.is_closed for client in peer.clients)


@pytest.mark.parametrize("profile", [None, PROFILE])
async def test_native_discovery_uses_only_explicit_operator_identity(
    gryphon_config: GryphonConfig,
    peer: Peer,
    profile: str | None,
) -> None:
    config = gryphon_config.model_copy(update={"ucp_agent_profile": profile})
    assert await discover_tools(config, ENDPOINT, 100000) == [tool()]
    for request in peer.requests:
        assert request.headers.get("ucp-agent") == (f'profile="{PROFILE}"' if profile else None)
        if request.method == "POST":
            assert json.loads(request.content)["method"] != "tools/call"
