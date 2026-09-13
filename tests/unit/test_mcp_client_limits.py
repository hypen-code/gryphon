"""Native MCP admission, malformed metadata, and whole-session budget regressions."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from gryphon.errors import ExecutionError, SecurityViolationError
from gryphon.security import mcp_client
from gryphon.security.mcp_client import discover_tools, invoke_tool, tool_fingerprint
from gryphon.security.mcp_protocol import parse_json
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from gryphon.config import GryphonConfig

URL = "https://shop.example.com/mcp"
TOOL: dict[str, Any] = {"name": "search", "inputSchema": {"type": "object"}}
INIT = {
    "protocolVersion": "2025-03-26",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "synthetic", "version": "1"},
}


async def _public(host: str, port: int) -> list[str]:
    """Return a fixed public address without external discovery."""
    return ["93.184.216.34"]


def _install(
    monkeypatch: pytest.MonkeyPatch, config: GryphonConfig, handle: Callable[[httpx.Request], httpx.Response]
) -> NetworkClient:
    """Install a real pinned client with only its low-level transport substituted."""
    client = NetworkClient(config, resolver=_public, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    return client


@pytest.mark.parametrize(
    "tool",
    [
        {},
        {**TOOL, "name": ""},
        {**TOOL, "name": "x" * 129},
        {**TOOL, "inputSchema": []},
        {**TOOL, "inputSchema": {"type": "array"}},
        {**TOOL, "outputSchema": []},
        {**TOOL, "outputSchema": {"type": "string"}},
        {**TOOL, "description": 1},
        {**TOOL, "inputSchema": {"type": "object", "enum": [float("inf")]}},
        {**TOOL, "inputSchema": {"type": "object", "default": "\ud800"}},
    ],
)
def test_mcp_client_fingerprint_malformed_metadata(tool: dict[str, Any]) -> None:
    with pytest.raises(ExecutionError, match="invalid tool metadata"):
        tool_fingerprint(tool)


@pytest.mark.parametrize("budget", [0, -1, True])
async def test_mcp_client_invalid_budget_no_client(
    budget: int, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    def unexpected(config: GryphonConfig) -> NetworkClient:
        """Fail if admission reaches a transport constructor."""
        pytest.fail("invalid budget constructed a client")

    monkeypatch.setattr(mcp_client, "NetworkClient", unexpected)
    with pytest.raises(ExecutionError, match="budget is invalid"):
        await discover_tools(gryphon_config, URL, budget)


async def test_mcp_client_invalid_arguments_no_network(gryphon_config: GryphonConfig) -> None:
    invalid: Any = []
    with pytest.raises(ExecutionError, match="invocation is invalid"):
        await invoke_tool(gryphon_config, URL, "search", invalid, expected_fingerprint="x", timeout=1, max_bytes=100)


@pytest.mark.parametrize("mode", ["mime", "notification", "init", "ack", "session"])
async def test_mcp_client_session_protocol_failures(
    mode: str, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        """Produce one independently malformed response in an otherwise valid session."""
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(200 if mode == "ack" else 202)
        if mode == "mime":
            return httpx.Response(200, text="not MCP")
        if mode == "notification":
            return httpx.Response(200, json={"jsonrpc": "2.0", "method": "notifications/message"})
        result: dict[str, Any] = {"tools": [TOOL]}
        headers = {"Mcp-Session-Id": "different" if mode == "session" else "original"}
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26"} if mode == "init" else INIT
            headers = {"Mcp-Session-Id": "original"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers)

    client = _install(monkeypatch, gryphon_config, handle)
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, URL, 10000)
    with pytest.raises(ExecutionError, match="closed"):
        await client.request("POST", URL)


async def test_mcp_client_notifications_aggregate_across_pages(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        """Each response fits locally but the session-wide notification count does not."""
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, Any] = INIT if body["method"] == "initialize" else {"tools": []}
        notifications = b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n' * 60
        record = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=notifications + b"data: " + record + b"\n\n"
        )

    _install(monkeypatch, gryphon_config, handle)
    with pytest.raises(ExecutionError, match="session budget exceeded"):
        await discover_tools(gryphon_config, URL, 100000)


async def test_mcp_client_dns_within_deadline(monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig) -> None:
    async def blocked(host: str, port: int) -> list[str]:
        """Remain pending until the caller's total deadline cancels DNS work."""
        await asyncio.Event().wait()
        return []

    client = NetworkClient(gryphon_config, resolver=blocked)
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    with pytest.raises(ExecutionError):
        await invoke_tool(
            gryphon_config, URL, "search", {}, expected_fingerprint=tool_fingerprint(TOOL), timeout=0.01, max_bytes=1000
        )
    with pytest.raises(ExecutionError, match="closed"):
        await client.request("POST", URL)


async def test_mcp_client_domain_policy_retained(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        """Reject any request reaching transport after a domain policy denial."""
        pytest.fail("denied hostname reached transport")

    config = gryphon_config.model_copy(update={"allowed_domains": ["other.example.com"]})
    _install(monkeypatch, config, unexpected)
    with pytest.raises(SecurityViolationError):
        await discover_tools(config, URL, 10000)


def test_mcp_client_json_finite_float_preserved() -> None:
    assert parse_json(b'{"value":1.25}') == {"value": 1.25}


async def test_mcp_client_failed_call_not_retried(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """Fail after receiving a simulated business call, with no safe retry signal."""
        nonlocal calls
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/call":
            calls += 1
            raise httpx.ReadError("private upstream diagnostic", request=request)
        result = INIT if body["method"] == "initialize" else {"tools": [TOOL]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    _install(monkeypatch, gryphon_config, handle)
    with pytest.raises(ExecutionError, match="^Upstream request failed$"):
        await invoke_tool(
            gryphon_config, URL, "search", {}, expected_fingerprint=tool_fingerprint(TOOL), timeout=1, max_bytes=10000
        )
    assert calls == 1


async def test_mcp_client_repeated_cancel_awaits_disconnect(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class ClosingClient(NetworkClient):
        """Hold disconnect long enough to inject repeated caller cancellation."""

        async def close(self) -> None:
            """Await controlled cleanup, then close the actual pinned HTTP resources."""
            entered.set()
            await release.wait()
            await super().close()

    def handle(request: httpx.Request) -> httpx.Response:
        """Provide a small metadata-only session with a finite empty catalog."""
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = INIT if body["method"] == "initialize" else {"tools": []}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    client = ClosingClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    task = asyncio.create_task(discover_tools(gryphon_config, URL, 10000))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ExecutionError, match="closed"):
        await client.request("POST", URL)
