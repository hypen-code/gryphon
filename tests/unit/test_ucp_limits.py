"""Structural, reference and malformed-input bounds for UCP discovery imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_ucp import LIMIT, PROFILE_URL, SCHEMA_URL, configuration, profile
from test_ucp import network as network

from gryphon.compiler import ucp, ucp_refs
from gryphon.errors import CompileError, SwaggerFetchError

if TYPE_CHECKING:
    from gryphon.security.network import NetworkClient


@pytest.mark.parametrize("limit", [0, -1, 1])
async def test_ucp_invalid_byte_budgets_reject_before_network(
    limit: int,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    with pytest.raises(CompileError, match="byte limit"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), limit)
    assert not requests


@pytest.mark.parametrize("value", [float("nan"), object(), "${ENV}"])
async def test_ucp_non_json_and_interpolation_profiles_rejected(
    value: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    advertised = profile()
    advertised["ignored"] = value
    with pytest.raises(CompileError):
        await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)
    assert not requests


@pytest.mark.parametrize("document", [[], {"openapi": "2.0"}, {"openapi": "3.1.0", "paths": []}])
async def test_ucp_non_openapi_bindings_reject_explicitly(
    document: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"] = document
    with pytest.raises(CompileError):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


@pytest.mark.parametrize("parameters", [{}, ["invalid"]])
async def test_ucp_malformed_parameters_reject_explicitly(
    parameters: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["parameters"] = parameters
    with pytest.raises(CompileError, match="parameter"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_no_read_operations_is_an_explicit_error(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    del documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"]["get"]
    with pytest.raises(CompileError, match="no supported"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_path_references_are_not_followed(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, requests, _ = network
    documents["/schemas/rest.openapi.json"]["paths"]["/checkout-sessions/{id}"] = {"$ref": "path.json"}
    with pytest.raises(CompileError, match="path item"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
    assert len(requests) == 1


@pytest.mark.parametrize("reference,success", [("#/schemas/0", True), ("#/schemas/01", False), ("#/scalar", False)])
async def test_ucp_pointer_array_indices_are_canonical(
    reference: str,
    success: bool,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/checkout.json"] = {"$ref": "types.json" + reference}
    documents["/schemas/types.json"] = {"schemas": [{"type": "string"}], "scalar": 1}
    if success:
        result = await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)
        assert result["paths"]
    else:
        with pytest.raises(CompileError):
            await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_reference_expansion_has_independent_node_budget(
    monkeypatch: pytest.MonkeyPatch,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    monkeypatch.setattr(ucp_refs, "_MAX_EXPANSION_NODES", 4)
    with pytest.raises(CompileError, match="structural limits"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_synchronous_work_checks_deadline(
    monkeypatch: pytest.MonkeyPatch,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    monkeypatch.setattr(ucp_refs, "MAX_SECONDS", 0.0)
    with pytest.raises(CompileError, match="time limit"):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


@pytest.mark.parametrize("content", [b"not json", b'{"value":NaN}'])
async def test_ucp_invalid_json_fetch_is_sanitized(
    content: bytes,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    documents, _, _ = network
    documents["/schemas/rest.openapi.json"] = httpx.Response(200, content=content)
    with pytest.raises(SwaggerFetchError):
        await ucp.profile_to_openapi(profile(), PROFILE_URL, configuration(), LIMIT)


async def test_ucp_operator_approved_cross_origin_schema_can_compile(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    _, requests, _ = network
    advertised = profile()
    advertised["ucp"]["services"]["dev.ucp.shopping"][0]["schema"] = SCHEMA_URL.replace(
        "merchant.example", "docs.example"
    )
    config = configuration(allowed_domains=["merchant.example", "docs.example"])
    result = await ucp.profile_to_openapi(advertised, PROFILE_URL, config, LIMIT)
    assert result["paths"]
    assert all(request.headers["host"] == "docs.example" for request in requests)


@pytest.mark.parametrize("legacy_service", [[], {"version": "2026-01-11", "rest": []}])
async def test_ucp_malformed_legacy_bindings_reject(
    legacy_service: Any,
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    advertised = profile("2026-01-11")
    advertised["ucp"]["services"]["dev.ucp.shopping"] = legacy_service
    with pytest.raises(CompileError):
        await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)


async def test_ucp_duplicate_legacy_capabilities_reject(
    network: tuple[dict[str, Any], list[httpx.Request], list[NetworkClient]],
) -> None:
    advertised = profile("2026-01-11")
    advertised["ucp"]["capabilities"] *= 2
    with pytest.raises(CompileError, match="ambiguous"):
        await ucp.profile_to_openapi(advertised, PROFILE_URL, configuration(), LIMIT)
