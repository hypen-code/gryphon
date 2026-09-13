"""Synthetic published-shape UCP adapter contracts with the real pinned network client."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest

from gryphon.compiler import ucp
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.ucp_profile import SUPPORTED_VERSIONS
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError, SwaggerFetchError
from gryphon.models import SwaggerSource
from gryphon.saas_catalog import validate_uploaded_document
from gryphon.security.network import NetworkClient

PROFILE_URL = "https://merchant.example/.well-known/ucp"
SCHEMA_URL = "https://merchant.example/schemas/rest.openapi.json"
LIMIT = 100000


def configuration(**kwargs: Any) -> GryphonConfig:
    """Use explicit settings without reading the operator's environment file."""
    options: dict[str, Any] = {"_env_file": None, **kwargs}
    return GryphonConfig(**options)


def profile(version: str = "2026-08-25") -> dict[str, Any]:
    """Build the actual legacy nested or modern keyed-array advertisement shape."""
    binding = {"schema": SCHEMA_URL, "endpoint": "https://merchant.example/ucp/v1"}
    capability = {"version": version, "schema": "https://merchant.example/schemas/checkout.json"}
    if version == "2026-01-11":
        return {
            "ucp": {
                "version": version,
                "services": {"dev.ucp.shopping": {"version": version, "rest": binding}},
                "capabilities": [{"name": "dev.ucp.shopping.checkout", **capability}],
            }
        }
    return {
        "ucp": {
            "version": version,
            "services": {"dev.ucp.shopping": [{"version": version, "transport": "rest", **binding}]},
            "capabilities": {"dev.ucp.shopping.checkout": [capability]},
        }
    }


def schema() -> dict[str, Any]:
    """Represent actual checkout operation IDs, endpoint placeholder and external schema binding."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Shopping", "version": "2026-08-25"},
        "servers": [{"url": "{endpoint}"}],
        "paths": {
            "/checkout-sessions/{id}": {
                "parameters": [{"$ref": "#/components/parameters/id"}],
                "get": {
                    "operationId": "get_checkout",
                    "responses": {
                        "200": {
                            "description": "Checkout",
                            "content": {"application/json": {"schema": {"$ref": "checkout.json"}}},
                        }
                    },
                },
                "put": {"operationId": "update_checkout"},
            },
            "/checkout-sessions": {"post": {"operationId": "create_checkout"}},
            "/orders/{id}": {"get": {"operationId": "get_order"}},
        },
        "components": {
            "parameters": {"id": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}}
        },
    }


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]]:
    """Inject only DNS and transport; retain URL, DNS address, byte and redirect enforcement."""
    documents: dict[str, Any] = {
        "/schemas/rest.openapi.json": schema(),
        "/schemas/checkout.json": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    requests: list[httpx.Request] = []
    clients: list[NetworkClient] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Use a documentation-only fake public address without making real connections."""
        return ["93.184.216.34"]

    def handle(request: httpx.Request) -> httpx.Response:
        """Record every actual network dispatch and return only synthetic documents."""
        requests.append(request)
        document = documents[request.url.path]
        if isinstance(document, httpx.Response):
            return document
        return httpx.Response(200, json=document)

    def factory(config: GryphonConfig) -> NetworkClient:
        """Keep the existing NetworkClient as the sole schema HTTP authority."""
        client = NetworkClient(config, resolver=resolve, transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    monkeypatch.setattr(ucp, "NetworkClient", factory)
    return documents, requests, clients


@pytest.mark.parametrize("version", sorted(SUPPORTED_VERSIONS))
async def test_ucp_published_profile_shapes_compile_read_only(
    version: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, clients = network
    original = copy.deepcopy(documents)
    result = await ucp.profile_to_openapi(profile(version), PROFILE_URL, configuration(), LIMIT)
    assert result["servers"] == [{"url": "https://merchant.example/ucp/v1"}]
    assert list(result["paths"]) == ["/checkout-sessions/{id}"]
    assert set(result["paths"]["/checkout-sessions/{id}"]) == {"get"}
    assert result["x-gryphon-ucp"]["supported_capabilities"] == ["dev.ucp.shopping.checkout"]
    encoded = validate_uploaded_document(result, LIMIT)
    assert "$ref" not in encoded
    parser = SwaggerParser(SwaggerSource(name="store", swagger_url="unused", is_read_only=True))
    parser._raw_doc = json.loads(encoded)
    operations = parser._parse_paths()
    assert operations[0].operation_id == "get_checkout"
    assert operations[0].parameters[0].name == "id"
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all("authorization" not in request.headers and "cookie" not in request.headers for request in requests)
    assert all(client._client.is_closed for client in clients)
    assert original == documents


async def test_ucp_advertisement_filter_and_warnings_are_deterministic(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    advertised = profile()
    advertised["ucp"]["services"]["dev.ucp.shopping"].append({"transport": "mcp"})
    advertised["ucp"]["capabilities"]["dev.ucp.shopping.catalog"] = [{"version": "2026-08-25"}]
    advertised["ucp"]["capabilities"]["dev.ucp.shopping.fulfillment"] = [
        {"version": "2026-08-25", "extends": "dev.ucp.shopping.checkout"}
    ]
    documents["/schemas/rest.openapi.json"]["paths"]["/catalog/search"] = {"post": {"operationId": "search_catalog"}}
    config = configuration(allow_writes=True, allowed_write_operations=["store.create_checkout"])
    first = await ucp.profile_to_openapi(advertised, PROFILE_URL, config, LIMIT)
    second = await ucp.profile_to_openapi(advertised, PROFILE_URL, config, LIMIT)
    assert first == second
    warnings = first["x-gryphon-ucp"]["warnings"]
    assert warnings == sorted(warnings)
    assert any("Non-REST" in warning for warning in warnings)
    assert any("catalog" in warning for warning in warnings)
    assert any("fulfillment" in warning for warning in warnings)
    assert all(set(item) == {"get"} for item in first["paths"].values())


@pytest.mark.parametrize(
    "capability,operation,path",
    [
        ("cart", "get_cart", "/carts/{id}"),
        ("order", "get_order", "/orders/{id}"),
    ],
)
async def test_ucp_actual_advertised_get_bindings_are_preserved(
    capability: str,
    operation: str,
    path: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    advertised = profile()
    advertised["ucp"]["capabilities"] = {f"dev.ucp.shopping.{capability}": [{"version": "2026-08-25"}]}
    checkout = documents["/schemas/rest.openapi.json"]["paths"].pop("/checkout-sessions/{id}")
    checkout["get"]["operationId"] = operation
    documents["/schemas/rest.openapi.json"]["paths"][path] = checkout
    result = await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)
    assert list(result["paths"]) == [path]
    assert result["paths"][path]["get"]["operationId"] == operation


@pytest.mark.parametrize("status", [301, 302, 307, 401, 500])
async def test_ucp_failed_fetches_close_without_redirects(
    status: int,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, clients = network
    documents["/schemas/rest.openapi.json"] = httpx.Response(status, headers={"Location": "http://127.0.0.1/"})
    with pytest.raises(SwaggerFetchError, match="network policy"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert len(requests) == 1
    assert all(client._client.is_closed for client in clients)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "2099-01-01"),
        ("version", None),
        ("services", []),
        ("services", {}),
        ("capabilities", []),
        ("capabilities", {"invalid": [{"version": "2026-08-25"}]}),
        ("capabilities", {"dev.ucp.shopping.checkout": [{"version": "2026-08-25"}] * 2}),
    ],
)
async def test_ucp_malformed_profiles_rejected_before_network(
    field: str,
    value: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    advertised = profile()
    advertised["ucp"][field] = value
    with pytest.raises(CompileError):
        await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)
    assert requests == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://merchant.example/schema",
        "https://user:pass@merchant.example/schema",
        "https://merchant.example/schema?token=secret",
        "https://evil.example/rest.json",
        "https://merchant.example/#frag",
    ],
)
async def test_ucp_unapproved_schema_urls_rejected_without_fetch(
    url: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    advertised = profile()
    advertised["ucp"]["services"]["dev.ucp.shopping"][0]["schema"] = url
    with pytest.raises((CompileError, SwaggerFetchError)):
        await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)
    assert requests == []


async def test_ucp_cancellation_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    client = NetworkClient(configuration())

    async def blocked(self: NetworkClient, *args: Any, **kwargs: Any) -> httpx.Response:
        """Pause the import at an awaited network boundary until the caller cancels."""
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(NetworkClient, "request", blocked)
    monkeypatch.setattr(ucp, "NetworkClient", lambda config: client)
    task = asyncio.create_task(ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._client.is_closed
