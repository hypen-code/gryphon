"""Real hosted MCP discovery, execution and immutable refresh for synthetic published-shape UCP."""

from __future__ import annotations

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

_PROFILE_PATH = "/.well-known/ucp"
_SCHEMA_PATH = "/schemas/shopping.openapi.json"
_AGENT = 'profile="https://platform.example/profile"'


def ucp_profile(expanded: bool = False) -> dict[str, Any]:
    """Advertise exact 2026-08-25 keyed-array capabilities and an explicit REST binding."""
    version = "2026-08-25"
    names = ["checkout", "cart", "order"] if expanded else ["checkout"]
    return {
        "ucp": {
            "version": version,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": version,
                        "transport": "rest",
                        "schema": "https://example.com" + _SCHEMA_PATH,
                        "endpoint": "https://example.com/ucp/v1",
                    }
                ]
            },
            "capabilities": {f"dev.ucp.shopping.{name}": [{"version": version}] for name in names},
        }
    }


def ucp_schema() -> dict[str, Any]:
    """Use actual GET IDs, explicit path IDs and a caller-supplied UCP-Agent header."""
    paths: dict[str, Any] = {}
    for name, resource in [("checkout", "checkout-sessions"), ("cart", "carts"), ("order", "orders")]:
        paths[f"/{resource}/{{id}}"] = {
            "get": {
                "operationId": f"get_{name}",
                "summary": f"Get {name}",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "UCP-Agent", "in": "header", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}, "total": {"type": "integer"}},
                                    "required": ["id", "total"],
                                }
                            }
                        },
                    }
                },
            }
        }
    paths["/checkout-sessions"] = {"post": {"operationId": "create_checkout"}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "UCP Shopping", "version": "2026-08-25"},
        "servers": [{"url": "{endpoint}"}],
        "paths": paths,
    }


@pytest.fixture
def ucp_network(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]]:
    """Mock only DNS and HTTP transport for profile, schema and broker API calls."""
    documents = {_PROFILE_PATH: ucp_profile(), _SCHEMA_PATH: ucp_schema()}
    seen: list[httpx.Request] = []
    clients: list[NetworkClient] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Keep DNS deterministic without contacting the network."""
        assert host == "example.com" and port == 443
        return ["93.184.216.34"]

    def transport(request: httpx.Request) -> httpx.Response:
        """Exercise pinned dispatch and return synthetic public documents or API responses."""
        seen.append(request)
        assert request.url.host == "93.184.216.34" and request.headers["host"] == "example.com"
        assert request.method == "GET" and "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path in documents:
            return httpx.Response(200, json=documents[request.url.path])
        assert request.url.path.startswith("/ucp/v1/checkout-sessions/")
        assert request.headers["ucp-agent"] == _AGENT
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1], "total": 7})

    def factory(config: GryphonConfig) -> NetworkClient:
        """Keep production URL validation, pinning, decoding and closure semantics intact."""
        client = NetworkClient(config, resolver=resolve, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    for module in ("gryphon.saas_spec_import", "gryphon.compiler.ucp", "gryphon.security.broker"):
        monkeypatch.setattr(module + ".NetworkClient", factory)
    return documents, seen, clients


async def _import_and_bind(http: httpx.AsyncClient, channel: dict[str, str]) -> tuple[dict[str, Any], int]:
    """Add a UCP source beside the existing catalog without changing channel authentication."""
    response = await http.post(
        channel["tenant"] + "/specs",
        json={
            "name": "shop",
            "url": "https://example.com",
            "kind": "ucp",
        },
    )
    assert response.status_code == 201
    spec = response.json()
    assert spec["source_type"] == "ucp_url" and spec["source_url"] == "https://example.com" + _PROFILE_PATH
    assert spec["diagnostics"]["available_operations"] == 1
    assert any("Non-GET" in warning for warning in spec["warnings"])
    spec = (await http.get(channel["tenant"] + "/specs/" + spec["id"])).json()
    response = await http.patch(
        channel["path"],
        json={
            "name": "weather",
            "spec_ids": [channel["spec"], spec["id"]],
            "sandbox_mode": "restricted",
            "allowed_imports": [],
            "enabled": True,
        },
    )
    assert response.status_code == 200
    return spec, int(response.json()["revision"])


async def _exercise(channel: dict[str, str]) -> str:
    """Discover real invocation metadata, execute a public GET and replay exact source."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        listing = await _data(client, "list_servers")
        assert {item["name"]: item["function_count"] for item in listing["servers"]} == {"weather": 1, "shop": 1}
        found = await _data(client, "search_functions", {"query": "get_checkout"})
        assert any(
            item["server_name"] == "shop" and item["function_name"] == "get_checkout" for item in found["functions"]
        )
        inspected = await _data(
            client, "get_functions", {"functions": [{"server_name": "shop", "function_name": "get_checkout"}]}
        )
        operation = inspected["functions"][0]
        assert operation["invocation"]["capability"] == "shop.get_checkout"
        assert {item["name"] for item in operation["parameters"] if item["required"]} == {"id", "UCP-Agent"}
        executed = await _data(
            client,
            "execute_code",
            {
                "code": (
                    'result = await call_tool("shop.get_checkout", {"id": inputs["id"], "UCP-Agent": inputs["agent"]})'
                ),
                "description": "Read public checkout",
                "inputs": {"id": "checkout-1", "agent": _AGENT},
            },
        )
        assert executed["success"] and executed["data"] == {"id": "checkout-1", "total": 7}
        replay = await _data(
            client,
            "run_cached_code",
            {"cache_id": executed["cache_id"], "params": {"id": "checkout-2", "agent": _AGENT}},
        )
        assert replay["success"] and replay["data"] == {"id": "checkout-2", "total": 7}
        return str(executed["cache_id"])


async def test_ucp_hosted_refresh_adds_capabilities_and_invalidates_stale_replay(
    hosted: tuple[httpx.AsyncClient, Starlette],
    ucp_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    """Refresh publishes newly advertised GETs, advances the bound revision and keeps the old document."""
    http, _ = hosted
    documents, seen, clients = ucp_network
    channel = await _channel(http, "weather")
    original, revision = await _import_and_bind(http, channel)
    cached = await _exercise(channel)
    documents[_PROFILE_PATH] = ucp_profile(expanded=True)
    path = channel["tenant"] + "/specs/" + original["id"]
    response = await http.post(path + "/refresh", json={"update_channels": True})
    assert response.status_code == 201
    updated = response.json()
    assert updated["parent_id"] == original["id"] and updated["source_type"] == "ucp_url"
    assert updated["diagnostics"]["available_operations"] == 3
    assert (await http.get(path)).json()["document"] == original["document"]
    bound = (await http.get(channel["tenant"] + "/channels")).json()["items"][0]
    assert bound["revision"] == revision + 1 and bound["spec_ids"] == [channel["spec"], updated["id"]]
    assert all(client._client.is_closed for client in clients)
    async with Client(channel["url"], auth=channel["token"]) as client:
        listing = await _data(client, "list_servers")
        assert next(item for item in listing["servers"] if item["name"] == "shop")["function_count"] == 3
        inspected = await _data(
            client,
            "get_functions",
            {
                "functions": [
                    {"server_name": "shop", "function_name": "get_cart"},
                    {"server_name": "shop", "function_name": "get_order"},
                ]
            },
        )
        assert {item["invocation"]["capability"] for item in inspected["functions"]} == {
            "shop.get_cart",
            "shop.get_order",
        }
        stale = await _data(client, "run_cached_code", {"cache_id": cached})
        assert not stale["success"] and stale["error_type"] == "conflict"
    assert sum(request.url.path == _PROFILE_PATH for request in seen) == 2
    assert sum(request.url.path == _SCHEMA_PATH for request in seen) == 2
    assert sum(request.url.path.startswith("/ucp/v1/") for request in seen) == 2


@pytest.mark.parametrize("unsupported", ["transport", "capability", "version"])
async def test_ucp_hosted_unsupported_import_and_refresh_leave_snapshots_and_bindings_unchanged(
    hosted: tuple[httpx.AsyncClient, Starlette],
    ucp_network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
    unsupported: str,
) -> None:
    """Unsupported advertisements fail closed on creation and refresh without partial version mutation."""
    http, _ = hosted
    documents, _, _ = ucp_network
    channel = await _channel(http, "weather")
    original, _ = await _import_and_bind(http, channel)
    before_specs = (await http.get(channel["tenant"] + "/specs")).json()
    before_channels = (await http.get(channel["tenant"] + "/channels")).json()
    profile = ucp_profile()
    if unsupported == "transport":
        profile["ucp"]["services"]["dev.ucp.shopping"][0]["transport"] = "mcp"
    elif unsupported == "capability":
        profile["ucp"]["capabilities"] = {"dev.ucp.shopping.catalog": [{"version": "2026-08-25"}]}
    else:
        profile["ucp"]["version"] = "2099-01-01"
    documents[_PROFILE_PATH] = profile
    response = await http.post(
        channel["tenant"] + "/specs", json={"name": "unsupported", "url": "https://example.com", "kind": "ucp"}
    )
    assert response.status_code == 400
    response = await http.post(
        channel["tenant"] + "/specs/" + original["id"] + "/refresh", json={"update_channels": True}
    )
    assert response.status_code == 400
    assert (await http.get(channel["tenant"] + "/specs")).json() == before_specs
    assert (await http.get(channel["tenant"] + "/channels")).json() == before_channels
