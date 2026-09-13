"""Regression contracts copied structurally from published UCP shopping REST bindings.

Canonical April/August GETs have optional auth/signing/protected headers, required
Request-Id and UCP-Agent, and oneOf success schemas. Network tests are synthetic;
separate read-only official-document smoke verifies live canonical documents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from test_ucp import LIMIT, PROFILE_URL, configuration, profile
from test_ucp import network as network

from gryphon.compiler import ucp
from gryphon.compiler.catalog import input_schema
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError, InputValidationError
from gryphon.models import EndpointManifest, SwaggerSource
from gryphon.saas_catalog import validate_uploaded_document
from gryphon.security.encoding import encode_request, validate_arguments

if TYPE_CHECKING:
    import httpx

    from gryphon.security.network import NetworkClient


def canonical_headers() -> list[dict[str, Any]]:
    """Preserve the names and requiredness of canonical April/August GET parameters."""
    names = [
        "Authorization",
        "X-API-Key",
        "Signature",
        "Signature-Input",
        "Request-Id",
        "User-Agent",
        "UCP-Agent",
        "Content-Type",
        "Accept",
        "Accept-Language",
        "Accept-Encoding",
    ]
    return [
        {"name": name, "in": "header", "required": name in {"Request-Id", "UCP-Agent"}, "schema": {"type": "string"}}
        for name in names
    ]


async def test_ucp_canonical_get_keeps_required_identity_without_generating_it(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    operation = documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]
    operation["parameters"] = canonical_headers()
    documents["/schemas/rest.openapi.json"]["components"]["schemas"] = {
        "checkout_response": {"oneOf": [{"$ref": "checkout.json"}, {"$ref": "error.json"}]}
    }
    operation["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/checkout_response"
    }
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    validate_uploaded_document(result, LIMIT)
    parser = SwaggerParser(SwaggerSource(name="store", swagger_url="unused", is_read_only=True))
    parser._raw_doc = result
    endpoint = parser._parse_paths()[0]
    assert {param.name for param in endpoint.parameters if param.required} == {"id", "Request-Id", "UCP-Agent"}
    assert {param.name for param in endpoint.parameters} == {
        "id",
        "Request-Id",
        "UCP-Agent",
        "User-Agent",
        "Accept",
        "Accept-Language",
    }
    assert endpoint.response_json_schema == {}
    assert len(requests) == 1
    assert any("not schema-validated" in warning for warning in result["x-gryphon-ucp"]["warnings"])
    manifest = EndpointManifest(
        function_name="get_checkout",
        summary="",
        method="GET",
        path=endpoint.path,
        parameters_summary="",
        response_summary="",
        parameters=endpoint.parameters,
        input_schema=input_schema(endpoint),
    )
    values = {
        "id": "checkout123",
        "Request-Id": "request123",
        "UCP-Agent": 'profile="https://platform.example/.well-known/ucp"',
    }
    arguments = validate_arguments(manifest, values)
    url, headers, _ = encode_request(manifest, "https://merchant.example/ucp/v1", arguments)
    assert url == "https://merchant.example/ucp/v1/checkout-sessions/checkout123"
    assert headers["UCP-Agent"] == values["UCP-Agent"]
    with pytest.raises(InputValidationError):
        validate_arguments(manifest, {"id": "checkout123", "Request-Id": "request123"})


@pytest.mark.parametrize(
    "schema",
    [
        {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        {"type": "string", "pattern": ".*"},
    ],
)
async def test_ucp_unsupported_response_semantics_are_explicitly_omitted(
    schema: dict[str, Any],
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/checkout.json"] = schema
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    response = result["paths"]["/checkout-sessions/{id}"]["get"]["responses"]["200"]
    assert "content" not in response
    assert any("not schema-validated" in warning for warning in result["x-gryphon-ucp"]["warnings"])


async def test_ucp_unsupported_nested_response_is_omitted_after_bounded_resolution(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    documents["/schemas/checkout.json"] = {"type": "object", "properties": {"id": {"$ref": "id.json"}}}
    documents["/schemas/id.json"] = {"type": "string", "pattern": ".*"}
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert len(requests) == 3
    assert "content" not in result["paths"]["/checkout-sessions/{id}"]["get"]["responses"]["200"]


async def test_ucp_unused_response_branches_and_annotations_are_not_fetched(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    operation = documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]
    operation["responses"]["400"] = {"$ref": "https://unapproved.example/error.json"}
    operation["responses"]["200"]["headers"] = {"Signature": {"$ref": "signature.json"}}
    operation["callbacks"] = {"unused": {"$ref": "callback.json"}}
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    retained = result["paths"]["/checkout-sessions/{id}"]["get"]
    assert set(retained["responses"]) == {"200"}
    assert "headers" not in retained["responses"]["200"]
    assert "callbacks" not in retained
    assert len(requests) == 2


@pytest.mark.parametrize("name", ["Request-Signature", "Content-Type", "Accept-Encoding"])
async def test_ucp_required_signing_or_protected_headers_are_not_silently_removed(
    name: str,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    operation = documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]
    operation["parameters"] = [{"name": name, "in": "header", "required": True, "schema": {"type": "string"}}]
    with pytest.raises(CompileError, match="required credentials, signing or broker-owned"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_unsupported_required_request_schema_still_fails_closed(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    parameter = documents["/schemas/rest.openapi.json"]["components"]["parameters"]["id"]
    parameter["schema"]["pattern"] = ".*"
    with pytest.raises(CompileError, match="supported OpenAPI schema subset"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_non_json_response_media_has_an_explicit_warning(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    response = documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]["responses"]["200"]
    response["content"] = {"text/plain": {"schema": {"type": "string"}}}
    result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert any("omitted" in warning for warning in result["x-gryphon-ucp"]["warnings"])


@pytest.mark.parametrize("responses", [[], {"200": {"content": ["invalid"]}}])
async def test_ucp_malformed_response_structure_is_rejected(
    responses: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]["responses"] = responses
    with pytest.raises(CompileError, match="Invalid UCP response"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
