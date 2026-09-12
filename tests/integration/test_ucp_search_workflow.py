"""Complete native UCP search, actionable failures, and offline reduction of stored results."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted
from test_saas_ucp_mcp import ENDPOINT, PROFILE, import_bound, native_profile, object_schema

from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig

AGENT_PROFILE = "https://caller.example/ucp-profile.json"
SEARCH = 'result = await call_tool("shop.search_catalog", {"json_body": inputs})'
PRIVATE = "Bearer synthetic-private-token password=not-public\ntraceback internal"


def search_tool() -> dict[str, Any]:
    """Require the same nested agent-profile path as the merchant's native search contract."""
    return {
        "name": "search_catalog",
        "description": "Search products. Prices are integer minor units.",
        "inputSchema": object_schema(
            {
                "meta": object_schema({"ucp-agent": object_schema({"profile": {"type": "string", "format": "uri"}})}),
                "catalog": object_schema({"query": {"type": "string"}}),
            }
        ),
    }


@dataclass
class SearchPeer:
    """A synthetic merchant that rejects missing identity and returns a large catalog otherwise."""

    calls: int = 0
    seen: list[httpx.Request] = field(default_factory=list)
    error_code: str | None = None
    products: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"title": f"Bedsheet {index}", "price": index, "description": "x" * 1500} for index in range(110)
        ]
    )

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Use only fixed discovery RPCs and one declared business capability, never live HTTP."""
        self.seen.append(request)
        assert request.url.host == "93.184.216.34"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.method == "GET":
            assert request.headers["host"] == "coolbudget.lk" and request.url.path == "/.well-known/ucp"
            return httpx.Response(200, json=native_profile())
        assert request.headers["host"] == "merchant.myshopify.com" and request.url.path == "/api/ucp/mcp"
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(200, json={})
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [search_tool()]}
        else:
            assert method == "tools/call" and body["params"]["name"] == "search_catalog"
            self.calls += 1
            profile = body["params"]["arguments"].get("meta", {}).get("ucp-agent", {}).get("profile")
            if self.error_code or profile != AGENT_PROFILE:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": -32001,
                            "message": PRIVATE * 1000,
                            "data": {
                                "code": self.error_code or "invalid_profile_url",
                                "content": PRIVATE,
                                "continue_url": "https://merchant.example/?token=private",
                            },
                        },
                    },
                )
            assert request.headers["ucp-agent"] == f'profile="{AGENT_PROFILE}"'
            result = {"content": [{"type": "text", "text": json.dumps({"products": self.products})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def search_peer(monkeypatch: pytest.MonkeyPatch) -> SearchPeer:
    """Retain the real network policy and transport codec underneath deterministic fake DNS."""
    peer = SearchPeer()

    async def resolve(host: str, port: int) -> list[str]:
        """Never resolve the reported merchant or operator profile through live DNS."""
        assert host in {"coolbudget.lk", "merchant.myshopify.com", "caller.example"} and port == 443
        return ["93.184.216.34"]

    def factory(config: GryphonConfig) -> NetworkClient:
        """Use production body bounds, TLS routing metadata and cookie isolation."""
        return NetworkClient(config, resolver=resolve, transport=httpx.MockTransport(peer.handle))

    for module in ("saas_spec_import", "security.mcp_client", "security.broker"):
        monkeypatch.setattr("gryphon." + module + ".NetworkClient", factory)
    monkeypatch.setattr("gryphon.security.network.resolve_addresses", resolve)
    return peer


async def bound(http: httpx.AsyncClient) -> dict[str, str]:
    """Publish the UCP catalog through actual authenticated browser endpoints."""
    channel = await _channel(http, "weather")
    spec, _ = await import_bound(http, channel, PROFILE)
    assert spec["resolved_endpoint"] == ENDPOINT
    return channel


async def test_missing_ucp_profile_is_actionable_and_secret_free(
    hosted: tuple[httpx.AsyncClient, Starlette],
    search_peer: SearchPeer,
) -> None:
    """The exact empty-profile report identifies the condition without revealing upstream text."""
    http, _ = hosted
    channel = await bound(http)
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(
            client,
            "execute_code",
            {
                "code": SEARCH,
                "description": "Search catalog",
                "inputs": {
                    "meta": {"ucp-agent": {"profile": ""}},
                    "catalog": {"query": "bedsheets"},
                },
            },
        )
        assert not result["success"]
        encoded = json.dumps(result)
        assert "invalid_profile_url" in encoded and "GRYPHON_UCP_AGENT_PROFILE" in result["error"]
        assert result["diagnostic"] == {"kind": "upstream", "phase": "invoke", "upstream_code": "invalid_profile_url"}
        assert "Request failed; check the arguments" not in result["error"]
        assert PRIVATE not in encoded and "continue_url" not in encoded and "traceback" not in encoded
        if result.get("run_id"):
            receipt = await _data(client, "get_run", {"run_id": result["run_id"]})
            assert receipt["result"]["error"] == result["error"]


async def project_saved(client: Client[Any], artifact_id: str) -> None:
    """Exercise multiple bounded projections and deny attempts to call an upstream capability."""
    projected = await _data(
        client,
        "transform_artifact",
        {
            "artifact_id": artifact_id,
            "description": "Summarize saved search",
            "code": 'result = {"count": len(inputs["artifact"]["products"]), '
            '"total": sum([p["price"] for p in inputs["artifact"]["products"]])}',
        },
    )
    assert projected["success"] and projected["data"] == {"count": 110, "total": sum(range(110))}
    assert not projected.get("cache_id")
    limited = await _data(
        client,
        "transform_artifact",
        {
            "artifact_id": artifact_id,
            "description": "Saved titles",
            "inputs": {"take": 2},
            "code": 'result = [p["title"] for p in inputs["artifact"]["products"][:inputs["params"]["take"]]]',
        },
    )
    assert limited["success"] and limited["data"] == ["Bedsheet 0", "Bedsheet 1"]
    denied = await _data(
        client,
        "transform_artifact",
        {
            "artifact_id": artifact_id,
            "description": "No network allowed",
            "code": SEARCH,
        },
    )
    assert not denied["success"] and denied["diagnostic"]["violation_type"] == "blocked_call"


async def test_configured_profile_large_search_projects_artifact_without_refetch(
    hosted: tuple[httpx.AsyncClient, Starlette],
    search_peer: SearchPeer,
) -> None:
    """One large merchant result supports multiple small local projections through real Monty."""
    http, app = hosted
    app.state.admin.base.ucp_agent_profile = AGENT_PROFILE
    app.state.runtimes._base.ucp_agent_profile = AGENT_PROFILE
    channel = await bound(http)
    assert search_peer.calls == 0
    async with Client(channel["url"], auth=channel["token"]) as client:
        inspected = await _data(
            client, "get_functions", {"functions": [{"server_name": "shop", "function_name": "search_catalog"}]}
        )
        operation = inspected["functions"][0]
        assert operation["ucp_agent_profile"]["operator_configured"] and operation["ucp_agent_profile"]["required"]
        assert 'json_body": inputs' in operation["usage_example"] and AGENT_PROFILE not in json.dumps(operation)
        result = await _data(
            client,
            "execute_code",
            {"code": SEARCH, "description": "Catalog search", "inputs": {"catalog": {"query": "bedsheets"}}},
        )
        assert result["success"] and result["truncated"] and result["artifact_id"]
        assert result["data"]["top_level_keys"] == ["products"]
        assert result["next"]["tool"] == "transform_artifact"
        assert search_peer.calls == 1
        requests = len(search_peer.seen)
        await project_saved(client, result["artifact_id"])
        assert search_peer.calls == 1 and len(search_peer.seen) == requests


@pytest.mark.parametrize("code", ["invalid_profile_url", "private-token", "x" * 10000, "invalid_profile_url\nsecret"])
async def test_native_rpc_diagnostics_never_echo_untrusted_error_content(
    hosted: tuple[httpx.AsyncClient, Starlette],
    search_peer: SearchPeer,
    code: str,
) -> None:
    """Known conditions have static diagnostics; hostile codes and arbitrary bodies remain private."""
    http, _ = hosted
    channel = await bound(http)
    search_peer.error_code = code
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(
            client,
            "execute_code",
            {
                "code": SEARCH,
                "description": "Rejected search",
                "inputs": {
                    "meta": {"ucp-agent": {"profile": AGENT_PROFILE}},
                    "catalog": {"query": "bedsheets"},
                },
            },
        )
    assert not result["success"] and result["error_type"] == "upstream"
    assert result["diagnostic"]["phase"] == "invoke"
    text = json.dumps(result)
    assert PRIVATE not in text and "continue_url" not in text and "private-token" not in text
    if code == "invalid_profile_url":
        assert "invalid_profile_url" in text
    else:
        assert code not in text


async def test_ast_failure_exposes_only_violation_kind_and_line(
    hosted: tuple[httpx.AsyncClient, Starlette],
    search_peer: SearchPeer,
) -> None:
    """The blocked __name__ attribute remains blocked but can be diagnosed without code disclosure."""
    http, _ = hosted
    channel = await bound(http)
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(
            client, "execute_code", {"code": "value = 1\nresult = type(value).__name__", "description": "Explain block"}
        )
    assert not result["success"] and result["error_type"] == "security"
    text = json.dumps(result)
    assert "blocked_attribute" in text and "__name__" not in text
    assert result["diagnostic"] == {"kind": "ast", "violation_type": "blocked_attribute", "line": 2}
    assert search_peer.calls == 0
