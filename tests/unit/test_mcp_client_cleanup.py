"""Owned-session DELETE cleanup is bounded, endpoint-local, and never a business retry."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from gryphon.errors import ExecutionError
from gryphon.security import mcp_client
from gryphon.security.mcp_client import discover_tools, invoke_tool, tool_fingerprint
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

URL = "https://shop.example/api/ucp/mcp"
TOOL: dict[str, Any] = {"name": "search", "inputSchema": {"type": "object"}}


async def _public(host: str, port: int) -> list[str]:
    """Return only a synthetic public destination without DNS traffic."""
    return ["93.184.216.34"]


class StatefulPeer(httpx.MockTransport):
    """Control protocol failure stages independently from simulated deletion behavior."""

    def __init__(self) -> None:
        """Keep remote state and transport lifecycle entirely inside this test."""
        super().__init__(self.handle)
        self.mode = "ok"
        self.session: str | None = "synthetic-owned"
        self.deletes: list[httpx.Request] = []
        self.business_calls = 0
        self.closed = False
        self.block_call = False
        self.call_entered = asyncio.Event()
        self.result: dict[str, Any] = {"structuredContent": {"items": []}}
        self.tools: list[dict[str, Any]] = [copy.deepcopy(TOOL)]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Offer one owned native session and separately controlled DELETE replies."""
        if request.method == "DELETE":
            self.deletes.append(request)
            return await self._delete_response(request)
        body = json.loads(request.content)
        headers = {"Set-Cookie": "ambient=discarded; Path=/"}
        result: dict[str, Any] = {"tools": self.tools}
        if body["method"] == "initialize":
            if self.session is not None:
                headers["Mcp-Session-Id"] = self.session
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
            if self.mode == "bad_initialize":
                result["capabilities"] = {}
        elif body["method"] == "notifications/initialized":
            return httpx.Response(200 if self.mode == "bad_ack" else 202)
        elif body["method"] == "tools/call":
            self.business_calls += 1
            self.call_entered.set()
            if self.block_call:
                await asyncio.Event().wait()
            result = self.result
        if self.mode == "session_drift" and body["method"] == "tools/list":
            headers["Mcp-Session-Id"] = "not-owned"
        response_id = "not-owned" if self.mode == "foreign_id" else body["id"]
        return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": response_id, "result": result})

    async def _delete_response(self, request: httpx.Request) -> httpx.Response:
        """Simulate supported, unsupported, oversized, forbidden and hung cleanup."""
        if self.mode == "unsupported":
            return httpx.Response(405)
        if self.mode == "redirect":
            return httpx.Response(307, headers={"Location": "http://127.0.0.1/private"})
        if self.mode == "oversize":
            return httpx.Response(200, content=b"x" * 1025)
        if self.mode == "error":
            raise httpx.ReadError("private cleanup diagnostic", request=request)
        if self.mode == "blocked":
            await asyncio.Event().wait()
        return httpx.Response(204)

    async def aclose(self) -> None:
        """Make local transport release observable after every cleanup attempt."""
        self.closed = True
        await super().aclose()


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig) -> StatefulPeer:
    """Keep NetworkClient and DNS policy real while substituting the numeric transport."""
    remote = StatefulPeer()
    client = NetworkClient(gryphon_config, resolver=_public, transport=remote)
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    return remote


async def _invoke(config: GryphonConfig, timeout: float = 1) -> Any:
    """Call only the local synthetic peer with imported native metadata."""
    return await invoke_tool(
        config, URL, "search", {}, expected_fingerprint=tool_fingerprint(TOOL), timeout=timeout, max_bytes=10000
    )


async def test_mcp_client_cleanup_fixed_endpoint_headers(peer: StatefulPeer, gryphon_config: GryphonConfig) -> None:
    assert await _invoke(gryphon_config) == {"items": []}
    assert len(peer.deletes) == 1 and peer.business_calls == 1 and peer.closed
    request = peer.deletes[0]
    assert request.url.host == "93.184.216.34" and request.url.path == "/api/ucp/mcp"
    assert request.headers["host"] == "shop.example"
    assert request.headers["mcp-session-id"] == "synthetic-owned"
    assert request.headers["mcp-protocol-version"] == "2025-03-26"
    assert "cookie" not in request.headers and "authorization" not in request.headers
    assert request.content == b""


async def test_mcp_client_metadata_session_cleanup(peer: StatefulPeer, gryphon_config: GryphonConfig) -> None:
    assert await discover_tools(gryphon_config, URL, 10000) == [TOOL]
    assert len(peer.deletes) == 1 and peer.business_calls == 0 and peer.closed


@pytest.mark.parametrize("mode", ["unsupported", "redirect", "oversize", "error", "blocked"])
async def test_mcp_client_cleanup_failure_preserves_success(
    mode: str, peer: StatefulPeer, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    peer.mode = mode
    monkeypatch.setattr(mcp_client, "MAX_CLEANUP_SECONDS", 0.02)
    assert await _invoke(gryphon_config) == {"items": []}
    assert len(peer.deletes) == 1 and peer.business_calls == 1 and peer.closed


async def test_mcp_client_cleanup_failure_preserves_primary_error(
    peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    peer.mode = "unsupported"
    peer.result = {"isError": True, "content": []}
    with pytest.raises(ExecutionError, match="^Remote MCP tool execution failed$"):
        await _invoke(gryphon_config)
    assert len(peer.deletes) == 1 and peer.business_calls == 1 and peer.closed


@pytest.mark.parametrize("mode", ["bad_initialize", "foreign_id"])
async def test_mcp_client_unverified_session_never_deleted(
    mode: str, peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    peer.mode = mode
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, URL, 10000)
    assert peer.deletes == [] and peer.closed


async def test_mcp_client_cleanup_rechecks_dns_policy(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    resolutions = 0

    async def rebinding(host: str, port: int) -> list[str]:
        """Offer a private address only after all four native POST requests complete."""
        nonlocal resolutions
        resolutions += 1
        return ["127.0.0.1"] if resolutions == 5 else ["93.184.216.34"]

    remote = StatefulPeer()
    client = NetworkClient(gryphon_config, resolver=rebinding, transport=remote)
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    assert await _invoke(gryphon_config) == {"items": []}
    assert resolutions == 5 and remote.deletes == [] and remote.business_calls == 1 and remote.closed


async def test_mcp_client_stateless_never_deleted(peer: StatefulPeer, gryphon_config: GryphonConfig) -> None:
    peer.session = None
    assert await discover_tools(gryphon_config, URL, 10000) == [TOOL]
    assert peer.deletes == [] and peer.closed


@pytest.mark.parametrize("mode", ["bad_ack", "session_drift"])
async def test_mcp_client_partially_failed_session_deletes_only_owned_id(
    mode: str, peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    peer.mode = mode
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, URL, 10000)
    assert len(peer.deletes) == 1
    assert peer.deletes[0].headers["mcp-session-id"] == "synthetic-owned"
    assert peer.closed


async def test_mcp_client_cancel_still_deletes_owned_session(peer: StatefulPeer, gryphon_config: GryphonConfig) -> None:
    peer.block_call = True
    task = asyncio.create_task(_invoke(gryphon_config))
    await peer.call_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(peer.deletes) == 1 and peer.business_calls == 1 and peer.closed


@pytest.mark.parametrize("operation", ["discover", "invoke"])
@pytest.mark.parametrize("field", ["description", "input_default", "output_default", "schema_key", "name"])
async def test_mcp_client_reflected_session_metadata_rejected_before_call(
    operation: str, field: str, peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    assert peer.session is not None
    tool = peer.tools[0]
    if field in ("description", "name"):
        tool[field] = peer.session
    elif field == "schema_key":
        tool["inputSchema"]["properties"] = {peer.session: {"type": "string"}}
    else:
        schema = "inputSchema" if field == "input_default" else "outputSchema"
        tool[schema] = {"type": "object", "properties": {"value": {"type": "string", "default": peer.session}}}
    with pytest.raises(ExecutionError, match="^Upstream response contained credential material$"):
        if operation == "discover":
            await discover_tools(gryphon_config, URL, 10000)
        else:
            await _invoke(gryphon_config)
    assert peer.business_calls == 0 and len(peer.deletes) == 1 and peer.closed


@pytest.mark.parametrize(
    "form",
    [
        "structured",
        "structured_key",
        "json_object",
        "json_array",
        "json_scalar",
        "json_escaped",
        "raw_text",
        "multimodal",
        "annotated",
    ],
)
async def test_mcp_client_reflected_session_result_rejected_and_cleaned(
    form: str, peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    assert peer.session is not None
    identifier = peer.session
    payloads: dict[str, dict[str, Any]] = {
        "structured": {"structuredContent": {"value": f"prefix {identifier} suffix"}},
        "structured_key": {"structuredContent": {identifier: "value"}},
        "json_object": {"content": [{"type": "text", "text": json.dumps({"value": identifier})}]},
        "json_array": {"content": [{"type": "text", "text": json.dumps([identifier])}]},
        "json_scalar": {"content": [{"type": "text", "text": json.dumps(identifier)}]},
        "json_escaped": {"content": [{"type": "text", "text": json.dumps(identifier).replace("s", "\\u0073", 1)}]},
        "raw_text": {"content": [{"type": "text", "text": f"prefix {identifier} suffix"}]},
        "multimodal": {
            "content": [
                {"type": "text", "text": "safe"},
                {"type": "resource_link", "uri": f"https://shop.example/{identifier}"},
            ]
        },
        "annotated": {"content": [{"type": "text", "text": "{}", "annotations": {"label": identifier}}]},
    }
    peer.result = payloads[form]
    with pytest.raises(ExecutionError, match="^Upstream response contained credential material$"):
        await _invoke(gryphon_config)
    assert peer.business_calls == 1 and len(peer.deletes) == 1 and peer.closed


async def test_mcp_client_expired_operation_allows_bounded_cleanup(
    peer: StatefulPeer, gryphon_config: GryphonConfig
) -> None:
    peer.block_call = True
    with pytest.raises(ExecutionError):
        await _invoke(gryphon_config, timeout=0.01)
    assert len(peer.deletes) == 1 and peer.business_calls == 1 and peer.closed
