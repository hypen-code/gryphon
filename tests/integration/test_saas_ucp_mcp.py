"""Real hosted MCP/Monty with synthetic Shopify-shaped native upstream HTTP only."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted

from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig

# Reproduce the reported user entry URL, but never resolve or contact that business.
ORIGIN = "https://coolbudget.lk"
ENDPOINT = "https://merchant.myshopify.com/api/ucp/mcp"
PROFILE = ORIGIN + "/.well-known/ucp"
VERSION = "2026-08-25"
READS = {"get_checkout", "get_cart", "get_order", "search_catalog", "lookup_catalog", "get_product"}
AGENT = "https://caller.example/agent-profile.json"
SEQUENCE = ["initialize", "notifications/initialized", "tools/list", "tools/list"]
CODE = (
    'response = await call_tool("shop.search_catalog", {"json_body": inputs})\n'
    'result = sum([product["price"] for product in response["products"]])'
)


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    """Require closed native objects, including nested caller-supplied UCP metadata."""
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def native_tool(name: str) -> dict[str, Any]:
    """Use native MCP schemas, not guessed methods from the advertised OpenRPC document."""
    arguments = object_schema(
        {
            "meta": object_schema({"ucp-agent": object_schema({"profile": {"type": "string"}})}),
            "catalog": object_schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}}),
        }
    )
    arguments["description"] = "Original raw native contract annotation"
    return {
        "name": name,
        "description": "Native " + name,
        "inputSchema": arguments,
        "outputSchema": object_schema(
            {"products": {"type": "array", "items": object_schema({"price": {"type": "integer"}})}}
        ),
        "annotations": {"readOnlyHint": True},
    }


def native_profile() -> dict[str, Any]:
    """Delegate to a different canonical host using standard capabilities, without vendor extensions."""
    capabilities = ["checkout", "cart", "order", "catalog.search", "catalog.lookup"]
    return {
        "ucp": {
            "version": VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": VERSION,
                        "transport": "mcp",
                        "endpoint": ENDPOINT,
                        "schema": "https://ucp.dev/2026-08-25/services/shopping/mcp.openrpc.json",
                    }
                ]
            },
            "capabilities": {"dev.ucp.shopping." + name: [{"version": VERSION}] for name in capabilities},
        }
    }


def arguments(limit: int = 2) -> dict[str, Any]:
    """Keep the platform identity entirely caller-provided inside the native input object."""
    return {"meta": {"ucp-agent": {"profile": AGENT}}, "catalog": {"query": "fixture", "limit": limit}}


@dataclass
class NativePeer:
    """Record every isolated upstream request while retaining production egress checks."""

    tools: list[dict[str, Any]] = field(default_factory=lambda: [native_tool(name) for name in sorted(READS)])
    seen: list[httpx.Request] = field(default_factory=list)
    clients: list[NetworkClient] = field(default_factory=list)
    failure: str = ""
    structured: bool = False

    def methods(self) -> list[str]:
        """Return native RPC methods separately from profile GETs."""
        return [json.loads(request.content)["method"] for request in self.seen if request.method == "POST"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Reject any unplanned network or credential propagation, including schema GETs."""
        self.seen.append(request)
        host = "coolbudget.lk" if request.method == "GET" else "merchant.myshopify.com"
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == host and request.extensions["sni_hostname"] == host
        assert not {"authorization", "cookie", "ucp-agent", "x-api-key"} & set(request.headers)
        if request.method == "GET":
            assert request.url.path in {"/.well-known/ucp", "/custom/profile.json"}
            return httpx.Response(200, json=native_profile(), headers={"Set-Cookie": "ignored=fixture"})
        assert request.method == "POST" and request.url.path == "/api/ucp/mcp"
        body = json.loads(request.content)
        assert body["jsonrpc"] == "2.0"
        if body["method"] == "notifications/initialized":
            assert "id" not in body
            return httpx.Response(200, json={})
        result = self.rpc(body)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"Set-Cookie": "ignored-native=fixture"},
        )

    def rpc(self, body: dict[str, Any]) -> dict[str, Any]:
        """Negotiate legacy native MCP, paginate tools, then return one JSON text block."""
        if body["method"] == "initialize":
            return {
                "protocolVersion": "unsupported-private-detail" if self.failure == "protocol" else "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "isolated-native-peer", "version": "1"},
            }
        if body["method"] == "tools/list":
            if not body["params"]:
                return {"tools": self.tools[:3], "nextCursor": "second-page"}
            assert body["params"] == {"cursor": "second-page"}
            return {"tools": self.tools[3:]}
        assert body["method"] == "tools/call"
        assert body["params"]["name"] in {"search_catalog", "cancel_cart"}
        supplied = body["params"]["arguments"]
        assert supplied == arguments(supplied["catalog"]["limit"])
        value = {"products": [{"price": 3}, {"price": 7}][: supplied["catalog"]["limit"]]}
        if self.failure == "isError":
            return {"isError": True, "content": [{"type": "text", "text": "private-upstream-detail"}]}
        content = [{"type": "text", "text": json.dumps(value)}]
        return {"structuredContent": value, "content": []} if self.structured else {"content": content}


@pytest.fixture
def native_peer(monkeypatch: pytest.MonkeyPatch) -> NativePeer:
    """Replace only DNS/transport factories, never importer, broker, MCP, or Monty execution."""
    peer = NativePeer()

    async def resolve(host: str, port: int) -> list[str]:
        """No live DNS, even for the reported merchant URL."""
        assert host in {"coolbudget.lk", "merchant.myshopify.com"} and port == 443
        return ["93.184.216.34"]

    def factory(config: GryphonConfig) -> NetworkClient:
        """Build independently owned production network clients for metadata and invocation."""
        client = NetworkClient(config, resolver=resolve, transport=httpx.MockTransport(peer.handle))
        peer.clients.append(client)
        return client

    for module in ("saas_spec_import", "security.mcp_client", "security.broker"):
        monkeypatch.setattr("gryphon." + module + ".NetworkClient", factory)
    return peer


async def import_bound(
    http: httpx.AsyncClient, channel: dict[str, str], url: str = ORIGIN + "/api/ucp/mcp"
) -> tuple[dict[str, Any], int]:
    """Import and bind the native snapshot through real scoped browser HTTP APIs."""
    response = await http.post(channel["tenant"] + "/specs", json={"name": "shop", "url": url, "kind": "ucp"})
    assert response.status_code == 201, response.text
    spec = response.json()
    spec = (await http.get(channel["tenant"] + "/specs/" + spec["id"])).json()
    policy = {
        "name": "Native shopping",
        "spec_ids": [spec["id"]],
        "sandbox_mode": "restricted",
        "allowed_imports": [],
        "enabled": True,
        "include_function_summaries": True,
    }
    bound = await http.patch(channel["path"], json=policy)
    assert bound.status_code == 200
    return spec, int(bound.json()["revision"])


async def execute(client: Client[Any], limit: int = 2) -> dict[str, Any]:
    """Actually reduce native JSON product values inside a fresh restricted Monty VM."""
    return await _data(
        client, "execute_code", {"code": CODE, "description": "Sum product prices", "inputs": arguments(limit)}
    )


async def inspect_catalog(client: Client[Any], names: set[str]) -> str:
    """Check hosted meta-tool summaries plus the authoritative nested native input contract."""
    listing = await _data(client, "list_servers")
    server = listing["servers"][0]
    assert server["name"] == "shop" and server["function_count"] == len(names)
    assert {row["name"] for row in server["functions"]} == names
    assert all(row["description"] for row in server["functions"])
    details = await _data(
        client, "get_functions", {"functions": [{"server_name": "shop", "function_name": "search_catalog"}]}
    )
    operation = details["functions"][0]
    assert operation["description"] and operation["invocation"]["capability"] == "shop.search_catalog"
    schema = operation["input_schema"]
    assert schema["required"] == ["json_body"]
    native = schema["properties"]["json_body"]
    assert native["required"] == ["meta", "catalog"]
    assert native["properties"]["meta"]["properties"]["ucp-agent"]["required"] == ["profile"]
    assert native["properties"]["catalog"]["required"] == ["query", "limit"]
    return str(listing["registry_fingerprint"])


@pytest.mark.parametrize("path", ["", "/.well-known/ucp", "/custom/profile.json", "/api/ucp/mcp"])
async def test_native_ucp_entrypoints_import_six_reads_without_business_calls(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, path: str
) -> None:
    """The actual reported URL shape resolves its profile then canonical cross-host MCP metadata."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    spec, _ = await import_bound(http, channel, ORIGIN + path)
    assert spec["source_url"] == ORIGIN + path and spec["source_type"] == "ucp_url"
    assert spec["resolved_profile_url"] == (ORIGIN + path if "profile.json" in path else PROFILE)
    assert spec["resolved_endpoint"] == ENDPOINT and spec["source_transport"] == "mcp"
    assert spec["diagnostics"]["available_operations"] == 6 and set(spec["mcp_bindings"]) == READS
    assert native_peer.methods() == SEQUENCE
    async with Client(channel["url"], auth=channel["token"]) as client:
        await inspect_catalog(client, READS)
    assert native_peer.methods() == SEQUENCE


@pytest.mark.parametrize("structured", [False, True])
async def test_native_ucp_hosted_executes_json_product_reduction_and_replays(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer, structured: bool
) -> None:
    """Native structuredContent and one JSON text block both reach actual Monty as JSON values."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    await import_bound(http, channel)
    native_peer.structured = structured
    assert native_peer.methods() == SEQUENCE
    async with Client(channel["url"], auth=channel["token"]) as client:
        await inspect_catalog(client, READS)
        result = await execute(client)
        assert result["success"] and result["data"] == 10, result
        replay = await _data(client, "run_cached_code", {"cache_id": result["cache_id"], "params": arguments(1)})
        assert replay["success"] and replay["data"] == 3, replay
    assert native_peer.methods() == SEQUENCE + (SEQUENCE + ["tools/call"]) * 2
    assert all(peer._client.is_closed for peer in native_peer.clients[-2:])


async def test_native_ucp_raw_schema_drift_conflicts_before_call_and_refreshes_same_sha(
    hosted: tuple[httpx.AsyncClient, Starlette], native_peer: NativePeer
) -> None:
    """An annotation stripped from OpenAPI still changes native authority and immutable version identity."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    original, revision = await import_bound(http, channel)
    path = channel["tenant"] + "/specs/" + original["id"]
    async with Client(channel["url"], auth=channel["token"]) as client:
        fingerprint = await inspect_catalog(client, READS)
        result = await execute(client)
        assert result["success"], result
        tool = next(tool for tool in native_peer.tools if tool["name"] == "search_catalog")
        tool["inputSchema"]["description"] = "Changed raw annotation; normalized OpenAPI remains identical"
        before = len(native_peer.methods())
        stale = await execute(client)
        replay = await _data(client, "run_cached_code", {"cache_id": result["cache_id"], "params": arguments()})
        for failed in (stale, replay):
            assert not failed["success"] and failed["error_type"] == "conflict", failed
        assert native_peer.methods()[before:] == SEQUENCE * 2
    refreshed = await http.post(path + "/refresh", json={"update_channels": True})
    assert refreshed.status_code == 201, refreshed.text
    updated = refreshed.json()
    assert updated["id"] != original["id"] and updated["parent_id"] == original["id"]
    assert updated["sha256"] == original["sha256"] and updated["mcp_bindings"] != original["mcp_bindings"]
    assert (await http.get(path)).json() == original
    bound = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert bound["revision"] == revision + 1 and bound["spec_ids"] == [updated["id"]]
    async with Client(channel["url"], auth=channel["token"]) as client:
        assert await inspect_catalog(client, READS) != fingerprint
        before = len(native_peer.seen)
        replay = await _data(client, "run_cached_code", {"cache_id": result["cache_id"], "params": arguments()})
        assert not replay["success"] and replay["error_type"] == "conflict", replay
        assert len(native_peer.seen) == before
        fresh = await execute(client)
        assert fresh["success"] and fresh["data"] == 10, fresh
    assert native_peer.methods().count("tools/call") == 2
