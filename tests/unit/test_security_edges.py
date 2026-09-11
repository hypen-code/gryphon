"""Adversarial regression tests for credentials, schemas, routing and revocation."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from gryphon.errors import ConfigurationError, ExecutionError, InputValidationError, SecurityViolationError
from gryphon.models import EndpointManifest, OAuth2AuthConfig, ParamSchema, StaticAuthConfig
from gryphon.security.ast_guard import ASTGuard
from gryphon.security.auth import AsyncVault
from gryphon.security.encoding import encode_request, validate_arguments
from gryphon.security.network import NetworkClient, PinnedTransport, resolve_addresses
from gryphon.security.policies import validated_url
from gryphon.security.response import validate_response
from gryphon.security.schema import validate_contract

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


def _endpoint(schema: dict[str, Any]) -> EndpointManifest:
    """Create a closed contract with full constraints and its original wire name."""
    return EndpointManifest(
        function_name="lookup",
        summary="",
        method="GET",
        path="/items",
        parameters_summary="",
        response_summary="",
        parameters=[ParamSchema(name="wire-name", location="query", param_type="array", json_schema=schema)],
        input_schema={
            "type": "object",
            "properties": {"wire-name": schema},
            "required": ["wire-name"],
            "additionalProperties": False,
        },
    )


async def _public(host: str, port: int) -> list[str]:
    """Return a deterministic public address without performing DNS."""
    return ["93.184.216.34"]


@pytest.mark.parametrize("url", ["https://@api.example.com", "https://:@api.example.com", "https://api.example.com:0"])
def test_empty_userinfo_and_zero_port_rejected(url: str) -> None:
    """Even empty credentials and invalid destination ports fail before canonicalization."""
    with pytest.raises(SecurityViolationError, match="^Invalid upstream URL$"):
        validated_url(url)


@pytest.mark.parametrize("source", ["import os", "from os import path"])
def test_explicit_offline_grant_cannot_enable_os(source: str) -> None:
    """Blocked system modules remain forbidden even if included in the requested grant."""
    with pytest.raises(SecurityViolationError):
        ASTGuard().validate(source, additional_allowed_modules=frozenset({"os", "numpy", "pandas"}))


@pytest.mark.parametrize(
    "schema, invalid",
    [
        ({"type": "integer", "minimum": 2}, 1),
        ({"type": "number", "exclusiveMaximum": 10}, 10),
        ({"type": "number", "multipleOf": 2}, 3),
        ({"type": "string", "minLength": 2, "maxLength": 4}, "x"),
        ({"type": "string", "enum": ["approved"]}, "unapproved"),
        ({"type": "boolean"}, 1),
        ({"type": "array", "items": {"type": "integer"}, "maxItems": 1}, [1, 2]),
        ({"type": "object", "additionalProperties": False}, {"unexpected": True}),
    ],
)
def test_input_and_output_enforce_normalized_constraints(schema: dict[str, Any], invalid: Any) -> None:
    """Full manifest constraints govern both directions without scalar coercion."""
    with pytest.raises(InputValidationError):
        validate_arguments(_endpoint(schema), {"wire-name": invalid})
    with pytest.raises(ExecutionError, match="^Upstream response violated its declared schema$"):
        validate_response(invalid, schema, {})


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_nested_schema_references_never_reach_validation(keyword: str) -> None:
    """Nested references are rejected regardless of local or remote target semantics."""
    schema = {"type": "object", "properties": {"nested": {keyword: "https://private.example.com/schema"}}}
    with pytest.raises(ValueError):
        validate_contract({}, schema)


def test_query_arrays_preserve_repeated_values_and_special_characters() -> None:
    """HTTPX receives the tuple form needed to preserve repeated wire keys safely."""
    endpoint = _endpoint({"type": "array", "items": {"type": "string"}})
    values = ["a&b", "x=y", "space here", "snowman \u2603", "a&b"]
    arguments = validate_arguments(endpoint, {"wire-name": values})
    url, _, _ = encode_request(endpoint, "https://api.example.com", arguments)
    assert httpx.URL(url).params.multi_items() == [("wire-name", value) for value in values]


@pytest.mark.parametrize("value", [{1: "not a JSON key"}, ("tuple",), {"value": float("inf")}])
def test_arguments_do_not_coerce_non_json_values(value: Any) -> None:
    """The structural walk rejects Python-only values before JSON encoding."""
    with pytest.raises(InputValidationError):
        validate_arguments(_endpoint({}), {"wire-name": value})


def test_cyclic_input_has_bounded_rejection() -> None:
    """Cyclic Python objects fail with a sanitized error rather than looping forever."""
    value: list[Any] = []
    value.append(value)
    with pytest.raises(InputValidationError):
        validate_arguments(_endpoint({}), {"wire-name": value})


@pytest.mark.parametrize("schema", [{"enum": list(range(65))}, {"const": {}}, {"enum": [[]]}, {"type": "invalid"}])
def test_unsafe_or_invalid_schema_fails_sanitized(schema: dict[str, Any]) -> None:
    """Equality work and malformed contracts cannot bypass schema admission."""
    with pytest.raises(ExecutionError, match="^Upstream response violated its declared schema$"):
        validate_response({}, schema, {})


def test_wide_output_has_bounded_rejection() -> None:
    """An empty output contract still applies the JSON node budget."""
    with pytest.raises(ExecutionError, match="declared schema"):
        validate_response([None] * 100001, {}, {})


def test_deep_schema_has_bounded_rejection() -> None:
    """Nested schema structures fail before invoking the recursive validator."""
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(33):
        schema = {"type": "array", "items": schema}
    with pytest.raises(ExecutionError, match="declared schema"):
        validate_response([], schema, {})


@pytest.mark.parametrize("name", ["x-cSrF", "X-Credential", "aUtHoRiZaTiOn", "cOoKiE"])
def test_credential_header_matching_is_case_insensitive(name: str) -> None:
    """Sensitive header aliases cannot hide exact credential reflections."""
    with pytest.raises(ExecutionError, match="credential material"):
        validate_response({"nested": ["mock-sensitive-value"]}, {}, {name: "mock-sensitive-value"})


@pytest.mark.parametrize("name", ["cookie", "COOKIE", "cOoKiE"])
async def test_explicit_cookie_header_case_is_preserved(gryphon_config: GryphonConfig, name: str) -> None:
    """Only explicitly supplied cookies cross the transport, regardless of header case."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Record the exact headers delivered to the numeric transport."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        await client.request("GET", "https://api.example.com", headers={name: "sid=mock-cookie"})
        assert requests[0].headers["Cookie"] == "sid=mock-cookie"
    finally:
        await client.close()


async def test_close_during_dns_cannot_create_connection_pool(gryphon_config: GryphonConfig) -> None:
    """An in-flight resolver cannot resurrect transport authority after close."""
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []

    async def blocked(host: str, port: int) -> list[str]:
        """Pause fake DNS until transport authority has been revoked."""
        entered.set()
        await release.wait()
        return ["93.184.216.34"]

    def handle(request: httpx.Request) -> httpx.Response:
        """Record any request accidentally sent after revocation."""
        requests.append(request)
        return httpx.Response(200, json={})

    transport = PinnedTransport(gryphon_config, blocked, httpx.MockTransport(handle))
    task = asyncio.create_task(transport.handle_async_request(httpx.Request("GET", "https://api.example.com")))
    try:
        await entered.wait()
        await transport.aclose()
        release.set()
        with pytest.raises(ExecutionError, match="closed"):
            await task
        assert not requests and not transport._pools
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await transport.aclose()


async def test_closed_network_rejects_request_sanitized(gryphon_config: GryphonConfig) -> None:
    """Post-close requests use the stable domain error rather than HTTPX internals."""
    client = NetworkClient(gryphon_config, resolver=_public)
    await client.close()
    with pytest.raises(ExecutionError, match="^Upstream network authority has been closed$"):
        await client.request("GET", "https://api.example.com")


@pytest.mark.parametrize("headers", [{"Cookie": "first", "cookie": "second"}, {"Authorization": "mock-\u00e9"}])
async def test_request_encoding_failures_do_not_reflect_headers(
    gryphon_config: GryphonConfig,
    headers: dict[str, str],
) -> None:
    """Ambiguous aliases and invalid header bytes never appear in diagnostics."""
    client = NetworkClient(gryphon_config, resolver=_public)
    try:
        with pytest.raises(ExecutionError, match="^Upstream request could not be encoded safely$"):
            await client.request("GET", "https://api.example.com", headers=headers)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "extras",
    [
        {"X-Tenant": "first", "x-tenant": "second"},
        {"X-Api-Key": "${UNRESOLVED_SECRET}"},
        {"X-Tenant": "x" * 65536},
    ],
)
async def test_invalid_extras_fail_before_auth_network(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
    extras: dict[str, str],
) -> None:
    """Malformed trusted headers fail before transmitting login credentials."""
    monkeypatch.setenv("GRYPHON_SVC_EXTRA_HEADERS", json.dumps(extras))
    client = NetworkClient(gryphon_config, resolver=_public)
    vault = AsyncVault(client)
    login = AsyncMock(return_value=({"Authorization": "Bearer mock-token"}, 3600.0))
    monkeypatch.setattr(vault, "_oauth", login)
    try:
        with pytest.raises(ConfigurationError):
            await vault.resolve(
                "svc", OAuth2AuthConfig(token_url="https://auth.example.com", client_id="c", client_secret="s")
            )
        login.assert_not_called()
    finally:
        vault.close()
        await client.close()


async def test_typed_auth_ignores_legacy_auth_and_cookie(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed credentials replace legacy authority but retain administrator extra headers."""
    monkeypatch.setenv("GRYPHON_SVC_AUTH", "Bearer legacy-token")
    monkeypatch.setenv("GRYPHON_SVC_COOKIE", "sid=legacy-cookie")
    monkeypatch.setenv("GRYPHON_SVC_EXTRA_HEADERS", '{"X-Tenant":"admin"}')
    client = NetworkClient(gryphon_config, resolver=_public)
    vault = AsyncVault(client)
    try:
        assert await vault.resolve("svc", StaticAuthConfig(value="Bearer typed-token")) == {
            "X-Tenant": "admin",
            "Authorization": "Bearer typed-token",
        }
    finally:
        vault.close()
        await client.close()


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan")])
async def test_nonpositive_or_nonfinite_network_budget_rejected(
    gryphon_config: GryphonConfig,
    duration: float,
) -> None:
    """Invalid deadlines fail without issuing DNS queries or opening connections."""
    client = NetworkClient(gryphon_config, resolver=_public)
    try:
        with pytest.raises(ExecutionError, match="^Upstream request budget is invalid$"):
            await client.request("GET", "https://api.example.com", timeout=duration)
    finally:
        await client.close()


@pytest.mark.parametrize("schema", [{"properties": {"x": []}}, {"items": []}, {"additionalProperties": "invalid"}])
def test_malformed_child_schemas_fail_closed(schema: dict[str, Any]) -> None:
    """Child schemas must obey the bounded local object or boolean representation."""
    with pytest.raises(ExecutionError, match="declared schema"):
        validate_response({}, schema, {})


def test_invalid_basic_token_does_not_prevent_benign_output() -> None:
    """An opaque static Basic header remains a known secret without unsafe decoding."""
    assert validate_response({"ok": True}, {}, {"Authorization": "Basic malformed!"}) == {"ok": True}


def test_secret_variant_count_is_bounded() -> None:
    """Excessive credential variants cannot trigger unbounded reflection scanning."""
    headers = {f"X-Api-Key-{index}": f"mock-token-{index}" for index in range(129)}
    with pytest.raises(ExecutionError, match="Authentication material exceeds supported limits"):
        validate_response({}, {}, headers)


def test_nonlocal_statement_is_blocked() -> None:
    """Nested scope manipulation remains denied by the shared static guard."""
    with pytest.raises(SecurityViolationError, match="blocked_nonlocal"):
        ASTGuard().validate("def outer():\n    x = 1\n    def inner():\n        nonlocal x\n        x = 2")


async def test_numeric_address_resolution_needs_no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric destinations bypass the resolver without bypassing transport policy."""
    resolver = AsyncMock(side_effect=AssertionError("unexpected DNS"))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await resolve_addresses("93.184.216.34", 443) == ["93.184.216.34"]


async def test_dns_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operating-system resolver diagnostics cannot expose configured hostnames."""
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=OSError("sensitive hostname")))
    with pytest.raises(SecurityViolationError, match="^Upstream hostname resolution failed$"):
        await resolve_addresses("api.example.com", 443)
