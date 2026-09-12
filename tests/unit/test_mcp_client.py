"""Isolated native MCP sessions through the real DNS-pinned HTTP boundary."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from gryphon.errors import ConflictError, ExecutionError, SecurityViolationError
from gryphon.security import mcp_client
from gryphon.security.mcp_client import discover_tools, invoke_tool, tool_fingerprint
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

ENDPOINT = "https://shop.example.com/api/ucp/mcp"
TOOL: dict[str, Any] = {
    "name": "search",
    "description": "Find items",
    "inputSchema": {"type": "object", "properties": {}},
    "outputSchema": {"type": "object", "properties": {"items": {"type": "array"}}},
}


async def _public(host: str, port: int) -> list[str]:
    """Return one deterministic public address without using DNS."""
    return ["93.184.216.34"]


class Server(httpx.MockTransport):
    """Stateful synthetic MCP peer, never a live business endpoint."""

    def __init__(self) -> None:
        """Initialize editable remote metadata and transport lifecycle counters."""
        super().__init__(self.handle)
        self.requests: list[dict[str, Any]] = []
        self.deletes: list[httpx.Request] = []
        self.headers: list[httpx.Headers] = []
        self.pages: list[dict[str, Any]] = [{"tools": [copy.deepcopy(TOOL)]}]
        self.page_index = 0
        self.result: dict[str, Any] = {"structuredContent": {"items": []}, "content": []}
        self.protocol = "2025-03-26"
        self.session: str | None = "synthetic-session"
        self.status = 200
        self.closed = 0
        self.delay = 0.0
        self.entered = asyncio.Event()
        self.clients: list[NetworkClient] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer only initialize, initialized, tools/list and tools/call."""
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "shop.example.com"
        assert request.extensions["sni_hostname"] == "shop.example.com"
        if request.method == "DELETE":
            self.deletes.append(request)
            return httpx.Response(204)
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        self.entered.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, headers={"Location": "http://127.0.0.1/private"})
        headers = {"Set-Cookie": "ambient=ignored; Path=/"}
        if body["method"] == "notifications/initialized":
            assert "id" not in body
            return httpx.Response(202, headers=headers)
        result = self._result(body)
        if body["method"] == "initialize" and self.session is not None:
            headers["Mcp-Session-Id"] = self.session
        return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _result(self, body: dict[str, Any]) -> dict[str, Any]:
        """Produce method-specific native protocol results."""
        if body["method"] == "initialize":
            self.page_index = 0
            assert body["params"]["capabilities"] == {}
            return {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
        if body["method"] == "tools/list":
            result = self.pages[min(self.page_index, len(self.pages) - 1)]
            self.page_index += 1
            return result
        assert body["method"] == "tools/call"
        return self.result

    async def aclose(self) -> None:
        """Record deterministic owned transport cleanup."""
        self.closed += 1
        await super().aclose()


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch) -> Server:
    """Inject only the transport and resolver, preserving the real security layer."""
    server = Server()

    def factory(config: GryphonConfig) -> NetworkClient:
        """Create a fresh owned pinned client on each API operation."""
        client = NetworkClient(config, resolver=_public, transport=server)
        server.clients.append(client)
        return client

    monkeypatch.setattr(mcp_client, "NetworkClient", factory)
    return server


async def _invoke(config: GryphonConfig, *, timeout: float = 1, max_bytes: int = 10000) -> Any:
    """Use the public invocation contract with an imported raw tool fingerprint."""
    return await invoke_tool(
        config,
        ENDPOINT,
        "search",
        {"query": "item"},
        expected_fingerprint=tool_fingerprint(TOOL),
        timeout=timeout,
        max_bytes=max_bytes,
    )


async def test_mcp_client_discovery_state_and_pagination(peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.pages = [{"tools": [TOOL], "nextCursor": "page2"}, {"tools": [{**TOOL, "name": "other"}]}]
    tools = await discover_tools(gryphon_config, ENDPOINT, 10000)
    assert [tool["name"] for tool in tools] == ["search", "other"]
    assert [request["method"] for request in peer.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    assert peer.requests[-1]["params"] == {"cursor": "page2"}
    assert "mcp-session-id" not in peer.headers[0]
    assert all(header["mcp-session-id"] == "synthetic-session" for header in peer.headers[1:])
    assert all(header["mcp-protocol-version"] == "2025-03-26" for header in peer.headers[1:])
    assert all("cookie" not in header and "authorization" not in header for header in peer.headers)
    assert peer.closed == 1


async def test_mcp_client_invocation_owns_fresh_sessions(peer: Server, gryphon_config: GryphonConfig) -> None:
    assert await _invoke(gryphon_config) == {"items": []}
    assert await _invoke(gryphon_config) == {"items": []}
    assert peer.closed == 2 and len(peer.clients) == 2
    calls = [body for body in peer.requests if body["method"] == "tools/call"]
    assert len(calls) == 2
    assert calls[0]["params"] == {"name": "search", "arguments": {"query": "item"}}
    ids = [body["id"] for body in peer.requests if "id" in body]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("change", ["name", "inputSchema", "outputSchema", "missing"])
async def test_mcp_client_drift_prevents_tool_call(change: str, peer: Server, gryphon_config: GryphonConfig) -> None:
    tool = peer.pages[0]["tools"][0]
    if change == "missing":
        peer.pages = [{"tools": []}]
    elif change == "name":
        tool[change] = "different"
    else:
        tool[change]["additionalProperties"] = False
    with pytest.raises(ConflictError, match="metadata changed"):
        await _invoke(gryphon_config)
    assert not any(body["method"] == "tools/call" for body in peer.requests)
    assert peer.closed == 1


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"content": [{"type": "text", "text": '{"items": [1]}'}]}, {"items": [1]}),
        (
            {"content": [{"type": "resource_link", "uri": "http://127.0.0.1/private"}]},
            [{"type": "resource_link", "uri": "http://127.0.0.1/private"}],
        ),
        ({"content": []}, []),
        ({"content": [], "structuredContent": {}}, {}),
    ],
)
async def test_mcp_client_content_preserved(
    result: dict[str, Any], expected: Any, peer: Server, gryphon_config: GryphonConfig
) -> None:
    peer.result = result
    assert await _invoke(gryphon_config) == expected
    assert len(peer.requests) == 4


@pytest.mark.parametrize(
    "result",
    [
        {"isError": True, "content": [{"type": "text", "text": "private upstream failure"}]},
        {"isError": "false", "content": []},
        {"structuredContent": []},
        {"content": "text"},
        {"content": [1]},
    ],
)
async def test_mcp_client_bad_result_is_static(
    result: dict[str, Any], peer: Server, gryphon_config: GryphonConfig
) -> None:
    peer.result = result
    with pytest.raises(ExecutionError) as error:
        await _invoke(gryphon_config)
    assert "private" not in str(error.value)
    assert sum(body["method"] == "tools/call" for body in peer.requests) == 1
    assert peer.closed == 1


@pytest.mark.parametrize(
    "pages",
    [
        [{"tools": [], "nextCursor": "same"}],
        [{"tools": [TOOL, TOOL]}],
        [{"tools": [], "nextCursor": 1}],
        [{"tools": [], "nextCursor": "x" * 4097}],
        [{"tools": [TOOL] * 1001}],
        [{"tools": [None]}],
    ],
)
async def test_mcp_client_listing_rejects_unbounded_metadata(
    pages: list[dict[str, Any]], peer: Server, gryphon_config: GryphonConfig
) -> None:
    peer.pages = pages
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, ENDPOINT, 500000)
    assert peer.closed == 1


async def test_mcp_client_pagination_page_limit(peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.pages = [{"tools": [], "nextCursor": str(index)} for index in range(101)]
    with pytest.raises(ExecutionError, match="pagination limit"):
        await discover_tools(gryphon_config, ENDPOINT, 100000)
    assert peer.page_index == 100


async def test_mcp_client_aggregate_includes_initialize(peer: Server, gryphon_config: GryphonConfig) -> None:
    await discover_tools(gryphon_config, ENDPOINT, 10000)
    init_id = peer.requests[0]["id"]
    init_result = {
        "protocolVersion": peer.protocol,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "synthetic", "version": "1"},
    }
    init_bytes = len(httpx.Response(200, json={"jsonrpc": "2.0", "id": init_id, "result": init_result}).content)
    with pytest.raises(ExecutionError, match="size limit"):
        await discover_tools(gryphon_config, ENDPOINT, init_bytes + 1)
    assert peer.closed == 2


@pytest.mark.parametrize("protocol", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_mcp_client_legacy_negotiation(protocol: str, peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.protocol = protocol
    peer.session = None
    assert await discover_tools(gryphon_config, ENDPOINT, 10000) == [TOOL]
    assert all("mcp-session-id" not in header for header in peer.headers)


async def test_mcp_client_unknown_version_rejected(peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.protocol = "future-invalid"
    with pytest.raises(ExecutionError, match="unsupported protocol"):
        await discover_tools(gryphon_config, ENDPOINT, 10000)
    assert len(peer.requests) == 1 and peer.closed == 1


@pytest.mark.parametrize("status", [301, 307, 401, 403, 404, 500])
async def test_mcp_client_http_errors_never_retry(status: int, peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.status = status
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, ENDPOINT, 10000)
    assert len(peer.requests) == 1 and peer.closed == 1


async def test_mcp_client_timeout_and_cleanup(peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.delay = 0.04
    with pytest.raises(ExecutionError):
        await _invoke(gryphon_config, timeout=0.1)
    assert len(peer.requests) == 3 and peer.closed == 1


async def test_mcp_client_cancel_closes_owned_client(peer: Server, gryphon_config: GryphonConfig) -> None:
    peer.delay = 10
    task = asyncio.create_task(_invoke(gryphon_config))
    await peer.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.closed == 1


async def test_mcp_client_private_dns_blocked(monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig) -> None:
    async def private(host: str, port: int) -> list[str]:
        """Supply a forbidden answer without performing DNS."""
        return ["127.0.0.1"]

    server = Server()
    client = NetworkClient(gryphon_config, resolver=private, transport=server)
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    with pytest.raises(SecurityViolationError):
        await discover_tools(gryphon_config, ENDPOINT, 10000)
    assert server.requests == []
    with pytest.raises(ExecutionError, match="closed"):
        await client.request("POST", ENDPOINT)


def test_mcp_client_fingerprint_exact_schemas_not_hints() -> None:
    changed = {**TOOL, "description": "different", "annotations": {"readOnlyHint": True}}
    assert tool_fingerprint(changed) == tool_fingerprint(TOOL)
    changed = copy.deepcopy(TOOL)
    changed["inputSchema"]["properties"]["x"] = {"type": "string"}
    assert tool_fingerprint(changed) != tool_fingerprint(TOOL)
    assert len(tool_fingerprint(TOOL)) == 64
