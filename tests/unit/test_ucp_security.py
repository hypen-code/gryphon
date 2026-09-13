"""Adversarial UCP schema imports never acquire credentials or unbounded reference authority."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from test_ucp import LIMIT, PROFILE_URL, SCHEMA_URL, configuration, profile
from test_ucp import network as network

from gryphon.compiler import ucp, ucp_refs
from gryphon.errors import CompileError, SwaggerFetchError
from gryphon.security.network import NetworkClient


@pytest.mark.parametrize(
    "reference",
    [
        "file:///etc/passwd",
        "https://other.example/private.json",
        "http://127.0.0.1/admin",
        "https://merchant.example/private?token=hidden",
        "#anchor",
        "#/does-not-exist",
        "bad\\path.json",
        " https://merchant.example/ignored.json",
        "https://user:pass@merchant.example/private.json",
    ],
)
async def test_ucp_malicious_external_refs_never_dispatch(
    reference: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, clients = network
    documents["/schemas/checkout.json"] = {"$ref": reference}
    with pytest.raises((CompileError, SwaggerFetchError)):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert [request.url.path for request in requests] == ["/schemas/rest.openapi.json", "/schemas/checkout.json"]
    assert all(client._client.is_closed for client in clients)


async def test_ucp_relative_references_are_cached_and_inlined(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    documents["/schemas/checkout.json"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://merchant.example/schemas/checkout.json",
        "type": "object",
        "properties": {"a": {"$ref": "types/id.json#/schemas/a~1b"}, "b": {"$ref": "types/id.json#/schemas/a~1b"}},
    }
    documents["/schemas/types/id.json"] = {"schemas": {"a/b": {"type": "string", "maxLength": 10}}}
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    response = result["paths"]["/checkout-sessions/{id}"]["get"]["responses"]["200"]
    properties = response["content"]["application/json"]["schema"]["properties"]
    assert properties["a"] == properties["b"] == {"type": "string", "maxLength": 10}
    assert len(requests) == 3


@pytest.mark.parametrize(
    "bad_schema,error",
    [
        ({"$ref": "checkout.json"}, "recursive"),
        ({"$ref": "#/a", "type": "string"}, "siblings"),
        ({"$id": "https://evil.example/rebased.json", "type": "string"}, "scope"),
        ({"$dynamicRef": "#node"}, "dynamic"),
        ({"$anchor": "node", "type": "string"}, "anchor"),
        ({"$schema": "https://example.com/unknown-dialect", "type": "string"}, "dialect"),
    ],
)
async def test_ucp_unsupported_semantics_fail_explicitly(
    bad_schema: dict[str, Any],
    error: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/checkout.json"] = bad_schema
    with pytest.raises(CompileError, match=error):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_hidden_ancestor_rebasing_is_rejected(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/checkout.json"] = {"$ref": "types.json#/$defs/parent/$defs/child"}
    documents["/schemas/types.json"] = {
        "$defs": {
            "parent": {
                "$id": "https://evil.example/",
                "$defs": {"child": {"type": "string"}},
            }
        }
    }
    with pytest.raises(CompileError, match="scope"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


@pytest.mark.parametrize("required", [True, False])
async def test_ucp_credential_headers_never_become_function_arguments(
    required: bool,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]["parameters"] = [
        {"in": "header", "name": "Authorization", "required": required, "schema": {"type": "string"}}
    ]
    if required:
        with pytest.raises(CompileError, match="credentials"):
            await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    else:
        result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
        params = result["paths"]["/checkout-sessions/{id}"]["get"]["parameters"]
        assert [parameter["name"] for parameter in params] == ["id"]


async def test_ucp_security_requirements_fail_closed(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"]["security"] = [{"bearer": []}]
    with pytest.raises(CompileError, match="authenticated"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_fetch_count_is_aggregate_bounded(
    monkeypatch: pytest.MonkeyPatch,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    monkeypatch.setattr(ucp_refs, "MAX_DOCUMENTS", 3)
    documents["/schemas/checkout.json"] = {"$ref": "next.json"}
    documents["/schemas/next.json"] = {"$ref": "last.json"}
    with pytest.raises(CompileError, match="fetch count"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert len(requests) == 3


async def test_ucp_total_schema_bytes_are_bounded(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, clients = network
    documents["/schemas/checkout.json"] = {"type": "string", "description": "a" * 5000}
    with pytest.raises(SwaggerFetchError, match="budgets"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), 3000)
    assert all(client._client.is_closed for client in clients)


async def test_ucp_total_timeout_closes_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = NetworkClient(configuration())

    async def delayed(self: NetworkClient, *args: Any, **kwargs: Any) -> httpx.Response:
        """Simulate a network request that stalls across the aggregate deadline."""
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(NetworkClient, "request", delayed)
    monkeypatch.setattr(ucp, "NetworkClient", lambda config: client)
    monkeypatch.setattr(ucp, "MAX_SECONDS", 0.01)
    with pytest.raises(CompileError, match="time limit"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert client._client.is_closed


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1"])
async def test_ucp_dns_policy_rejects_private_and_metadata(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def resolve(host: str, port: int) -> list[str]:
        """Return an attacker-controlled DNS response that must never reach transport."""
        return ["93.184.216.34", address]

    def handle(request: httpx.Request) -> httpx.Response:
        """Record unexpected dispatches as a test failure."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(configuration(), resolver=resolve, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(ucp, "NetworkClient", lambda config: client)
    with pytest.raises(SwaggerFetchError):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert not requests
    assert client._client.is_closed


async def test_ucp_domain_allowlist_restricts_even_official_schema_authority(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    advertised = profile()
    advertised["ucp"]["services"]["dev.ucp.shopping"][0]["schema"] = SCHEMA_URL.replace("merchant.example", "ucp.dev")
    with pytest.raises(SwaggerFetchError):
        await ucp.profile_to_openapi(
            advertised, PROFILE_URL, configuration(allowed_domains=["merchant.example"]), LIMIT
        )
    assert requests == []
