"""Provider acknowledgement compatibility using synthetic native MCP responses."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gryphon.errors import ExecutionError, SecurityViolationError
from gryphon.security import mcp_client
from gryphon.security.mcp_client import discover_tools, invoke_tool, tool_fingerprint
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


async def test_shopify_empty_json_200_notification_acknowledgement(gryphon_config: GryphonConfig) -> None:
    """The observed Shopify acknowledgement must not prevent otherwise valid tool discovery."""
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Emulate metadata only, never invoke an upstream business operation."""
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(200, json={})
        result = (
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
            if body["method"] == "initialize"
            else {"tools": [{"name": "search_catalog", "inputSchema": {"type": "object", "properties": {}}}]}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def factory(config: GryphonConfig) -> NetworkClient:
        """Retain the real pinned transport and bounded HTTP request implementation."""
        return NetworkClient(
            config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(transport)
        )

    with patch("gryphon.security.mcp_client.NetworkClient", factory):
        tools = await discover_tools(gryphon_config, "https://shop.example/api/ucp/mcp", 8192)
    assert tools[0]["name"] == "search_catalog"
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


URL = "https://shop.example/api/ucp/mcp"
TOOL: dict[str, Any] = {"name": "search_catalog", "inputSchema": {"type": "object"}}


def _peer(
    monkeypatch: pytest.MonkeyPatch,
    config: GryphonConfig,
    result: dict[str, Any],
    ack: httpx.Response,
    *,
    clients: list[NetworkClient] | None = None,
) -> list[str]:
    """Install an isolated stateless peer under the real pinned HTTP security layer."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Respond only to expected native MCP methods without resource resolution."""
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "notifications/initialized":
            return ack
        response_result = result
        if body["method"] == "initialize":
            response_result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif body["method"] == "tools/list":
            response_result = {"tools": [TOOL]}
        else:
            assert body["method"] == "tools/call"
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": response_result}
        return httpx.Response(200, headers={"Content-Type": "application/json"}, content=json.dumps(payload).encode())

    client = NetworkClient(
        config, resolver=AsyncMock(return_value=["93.184.216.34"]), transport=httpx.MockTransport(handle)
    )
    if clients is not None:
        clients.append(client)
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    return calls


async def _invoke(config: GryphonConfig) -> Any:
    """Call one synthetic native tool with its exact imported schema binding."""
    return await invoke_tool(
        config, URL, "search_catalog", {}, expected_fingerprint=tool_fingerprint(TOOL), timeout=1, max_bytes=10000
    )


@pytest.mark.parametrize(
    "ack",
    [
        httpx.Response(202),
        httpx.Response(204),
        httpx.Response(200, json={}),
        httpx.Response(200, headers={"Content-Type": "Application/JSON; charset=utf-8"}, content=b" { } \n"),
    ],
)
async def test_shopify_supported_acknowledgements(
    ack: httpx.Response, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    calls = _peer(monkeypatch, gryphon_config, {"content": []}, ack)
    assert await discover_tools(gryphon_config, URL, 10000) == [TOOL]
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


@pytest.mark.parametrize(
    "ack",
    [
        httpx.Response(200),
        httpx.Response(201),
        httpx.Response(202, json={}),
        httpx.Response(204, json={}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json=[]),
        httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"null"),
        httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"{}"),
        httpx.Response(200, text="<html>not a protocol acknowledgement</html>"),
        httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"x":1,"x":2}'),
        httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"{} {}"),
        httpx.Response(200, json={"padding": "x" * 10001}),
    ],
)
async def test_shopify_unrelated_acknowledgements_rejected(
    ack: httpx.Response, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    calls = _peer(monkeypatch, gryphon_config, {"content": []}, ack)
    with pytest.raises(ExecutionError):
        await discover_tools(gryphon_config, URL, 10000)
    assert calls == ["initialize", "notifications/initialized"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"items":[{"price":5}]}', {"items": [{"price": 5}]}),
        ("[1, 2, null]", [1, 2, None]),
        ("null", None),
        ("42", 42),
        ("-1.25", -1.25),
        ("true", True),
        ("false", False),
        ('"value"', "value"),
    ],
)
async def test_shopify_single_plain_json_text_normalized(
    text: str, expected: Any, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    calls = _peer(
        monkeypatch, gryphon_config, {"content": [{"type": "text", "text": text}]}, httpx.Response(200, json={})
    )
    assert await _invoke(gryphon_config) == expected
    assert calls == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


@pytest.mark.parametrize(
    "text",
    [
        "ordinary text",
        "NaN",
        "Infinity",
        "-Infinity",
        "1e999",
        '{"value":NaN}',
        '{"x":1,"x":2}',
        "{} {}",
        "[invalid]",
        "\ud800",
        "[" * 2000,
    ],
)
async def test_shopify_invalid_or_ambiguous_json_preserves_text(
    text: str, monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    result = {"content": [{"type": "text", "text": text}]}
    _peer(monkeypatch, gryphon_config, result, httpx.Response(200, json={}))
    assert await _invoke(gryphon_config) == result["content"]


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": "{}"}, {"type": "text", "text": "[]"}],
        [{"type": "text", "text": "{}", "annotations": {"audience": ["user"]}}],
        [{"type": "text", "text": "{}"}, {"type": "resource_link", "uri": "http://127.0.0.1/private"}],
        [{"type": "image", "text": "{}"}],
        [{"type": "text", "text": 1}],
    ],
)
async def test_shopify_multimodal_or_annotated_blocks_preserved(
    content: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    calls = _peer(monkeypatch, gryphon_config, {"content": content}, httpx.Response(200, json={}))
    assert await _invoke(gryphon_config) == content
    assert len(calls) == 4


async def test_shopify_structured_content_precedes_text(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    result = {"structuredContent": {"items": []}, "content": [{"type": "text", "text": '{"items":[1]}'}]}
    _peer(monkeypatch, gryphon_config, result, httpx.Response(200, json={}))
    assert await _invoke(gryphon_config) == {"items": []}


@pytest.mark.parametrize("phase", ["notifications/initialized", "tools/list", "tools/call"])
async def test_mcp_revocation_between_protocol_stages_stops_later_calls(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig, phase: str
) -> None:
    """A revoked broker scope cannot reach a business call after waiting on metadata discovery."""
    clients: list[NetworkClient] = []
    calls = _peer(
        monkeypatch, gryphon_config, {"structuredContent": {"items": []}}, httpx.Response(200, json={}), clients=clients
    )

    def active() -> None:
        """Model synchronous revocation as the selected async protocol step completes."""
        if calls and calls[-1] == phase:
            raise SecurityViolationError("Execution authority revoked")

    with pytest.raises(SecurityViolationError):
        await invoke_tool(
            gryphon_config,
            URL,
            "search_catalog",
            {},
            expected_fingerprint=tool_fingerprint(TOOL),
            timeout=1,
            max_bytes=10000,
            check_active=active,
        )
    assert calls[-1] == phase
    assert calls.count("tools/call") == (1 if phase == "tools/call" else 0)
    assert clients[0]._client.is_closed
