"""Adapt verified UCP profile shapes into the existing read-only OpenAPI compiler subset.

The mappings are from https://ucp.dev/2026-08-25/services/shopping/rest.openapi.json
and the corresponding published January and April bindings. Routes are always read
from the advertised schema, never synthesized from capability names. This adapter
is not a UCP platform implementation: negotiation, signatures, payments, extensions
and non-GET operations are unsupported.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from gryphon.compiler.documents import DEFAULT_MAX_DOCUMENT_BYTES
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.ucp_profile import (
    SHOPPING_SERVICE,
    SUPPORTED_VERSIONS,
    bounded_json,
    capabilities,
    origin,
    secure_url,
    shopping_binding,
)
from gryphon.compiler.ucp_refs import MAX_SECONDS, ReferenceLoader
from gryphon.compiler.ucp_responses import response_contracts
from gryphon.errors import CompileError, ExecutionError, SecurityViolationError, SwaggerFetchError
from gryphon.models import SwaggerSource
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.security.encoding import validate_header
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

_OPERATIONS = {
    "get_checkout": "dev.ucp.shopping.checkout",
    "get_cart": "dev.ucp.shopping.cart",
    "get_order": "dev.ucp.shopping.order",
}
_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-api-key", "signature", "signature-input", "request-signature"}
)


async def profile_to_openapi(
    profile: dict[str, Any], profile_url: str, config: GryphonConfig, max_bytes: int, *, profile_bytes: int = 0
) -> dict[str, Any]:
    """Import advertised public GET capabilities with aggregate network and output bounds.

    Args:
        profile: Already fetched JSON business discovery profile, treated as untrusted.
        profile_url: Absolute HTTPS URL from which the profile was obtained.
        config: Explicit host-owned network policy, never consulted for credentials.
        max_bytes: Aggregate profile/schema byte budget and separate output byte ceiling.

    Returns:
        Self-contained OpenAPI with deterministic x-gryphon-ucp warnings and capability metadata.

    Raises:
        CompileError: Unsupported profile, transport, schema semantics, or import bounds.
        SwaggerFetchError: A schema fetch violates network policy or fails securely.
    """
    limit = min(max_bytes, config.max_spec_size_bytes, DEFAULT_MAX_DOCUMENT_BYTES)
    if limit <= 0:
        raise CompileError("UCP import byte limit must be positive")
    remaining = limit - max(profile_bytes, bounded_json(profile, limit))
    warnings = {"Only public GET operations are imported; negotiation, signatures and payments are unsupported"}
    version, advertised, binding = _profile(profile, warnings)
    try:
        profile_url = secure_url(profile_url, config)
        schema_url, endpoint = _binding_urls(binding, profile_url, config)
        client = NetworkClient(config)
        try:
            async with asyncio.timeout(min(MAX_SECONDS, config.http_timeout_seconds)):
                loader = ReferenceLoader(client, config, remaining, schema_url)
                document = await loader.document(schema_url)
                result = await _adapt(document, loader, schema_url, endpoint, version, advertised, warnings)
                bounded_json(result, limit)
                loader.check_budget()
                return result
        finally:
            await finish_cleanup(client.close())
    except TimeoutError:
        raise CompileError("UCP import exceeded its total time limit") from None
    except (ExecutionError, SecurityViolationError, httpx.HTTPError, OSError):
        raise SwaggerFetchError("UCP schema fetch failed within configured network policy and budgets") from None


def _profile(
    profile: dict[str, Any], warnings: set[str]
) -> tuple[str, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Validate exact published profile versions without following version fallback URLs."""
    ucp = profile.get("ucp")
    if not isinstance(ucp, dict) or not isinstance(ucp.get("version"), str):
        raise CompileError("UCP profile requires protocol metadata and a version")
    version = ucp["version"]
    if version not in SUPPORTED_VERSIONS:
        raise CompileError("Unsupported UCP profile version")
    advertised = capabilities(ucp, version)
    binding = shopping_binding(ucp, version, warnings)
    return version, advertised, binding


def _binding_urls(binding: dict[str, Any], profile_url: str, config: GryphonConfig) -> tuple[str, str]:
    """Approve explicit schema authorities without granting arbitrary profile-driven egress."""
    schema = secure_url(binding.get("schema"), config)
    endpoint = secure_url(binding.get("endpoint"), config).rstrip("/")
    approved = origin(schema) in {origin(profile_url), ("https", "ucp.dev", 443)}
    if not approved and not config.allowed_domains:
        raise CompileError("Cross-origin UCP schemas require an explicit operator domain allowlist")
    return schema, endpoint


def _eligible(advertised: dict[str, list[dict[str, Any]]], version: str, warnings: set[str]) -> set[str]:
    """Match exact capability versions; extension advertisements do not grant base operations."""
    result: set[str] = set()
    for name, records in advertised.items():
        matching = [item for item in records if item["version"] == version and not item.get("extends")]
        supported = name in _OPERATIONS.values() and (
            name.endswith(".checkout") or version in {"2026-04-08", "2026-08-25"}
        )
        if matching and supported:
            result.add(name)
        else:
            warnings.add(f"Unsupported capability or version: {name}")
    return result


async def _adapt(
    document: dict[str, Any],
    loader: ReferenceLoader,
    schema_url: str,
    endpoint: str,
    version: str,
    advertised: dict[str, list[dict[str, Any]]],
    warnings: set[str],
) -> dict[str, Any]:
    """Keep advertised GET contracts, override schema server placeholders and validate compilation."""
    dialect = document.get("openapi")
    if not isinstance(dialect, str) or not dialect.startswith(("3.0.", "3.1.")):
        raise CompileError("UCP REST binding requires OpenAPI 3.0 or 3.1 JSON")
    if not isinstance(document.get("paths"), dict):
        raise CompileError("UCP REST binding requires an OpenAPI paths object")
    eligible = _eligible(advertised, version, warnings)
    paths, supported = await _paths(document, loader, schema_url, eligible, warnings)
    for name in eligible - supported:
        warnings.add(f"No supported advertised GET operation: {name}")
    if not paths:
        raise CompileError("UCP profile has no supported advertised public GET operations")
    result = {
        "openapi": dialect,
        "info": {"title": "UCP Shopping read-only capabilities", "version": version},
        "servers": [{"url": endpoint}],
        "paths": paths,
        "x-gryphon-ucp": {
            "version": version,
            "service": SHOPPING_SERVICE,
            "advertised_capabilities": sorted(advertised),
            "supported_capabilities": sorted(supported),
            "warnings": sorted(warnings),
        },
    }
    parser = SwaggerParser(SwaggerSource(name="ucp", swagger_url="ucp-adapted", is_read_only=True))
    parser._raw_doc = result
    try:
        parser._parse_paths()
    except (CompileError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CompileError("UCP REST contract is outside Gryphon's supported OpenAPI schema subset") from None
    return result


async def _paths(
    document: dict[str, Any],
    loader: ReferenceLoader,
    schema_url: str,
    eligible: set[str],
    warnings: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Read actual operation IDs and paths; never infer routes from descriptive metadata."""
    paths: dict[str, Any] = {}
    supported: set[str] = set()
    for path, item in sorted(document["paths"].items()):
        if not isinstance(path, str) or not isinstance(item, dict) or "$ref" in item:
            raise CompileError("UCP path item references and malformed paths are unsupported")
        if any(method in item for method in ("post", "put", "patch", "delete")):
            warnings.add("Non-GET operations are excluded, including POST catalog queries")
        operation = item.get("get")
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operationId")
        capability = _OPERATIONS.get(operation_id) if isinstance(operation_id, str) else None
        if capability not in eligible:
            warnings.add("Unadvertised or unmapped GET operations are excluded")
            continue
        parameters = await _parameters(item, operation, loader, schema_url, warnings)
        selected = {
            key: value
            for key, value in operation.items()
            if key in {"operationId", "summary", "description", "requestBody", "security"}
        }
        if selected.get("security", document.get("security")):
            raise CompileError("UCP authenticated REST operations are unsupported")
        selected["parameters"] = parameters
        selected["tags"] = [capability]
        expanded = await loader.expand(selected, schema_url)
        expanded["responses"] = await response_contracts(operation, loader, schema_url, warnings)
        paths[path] = {"get": expanded}
        if capability is not None:
            supported.add(capability)
    return paths, supported


async def _parameters(
    item: dict[str, Any],
    operation: dict[str, Any],
    loader: ReferenceLoader,
    schema_url: str,
    warnings: set[str],
) -> list[dict[str, Any]]:
    """Never expose credentials as callable arguments or discard required authentication."""
    result = []
    for scope in (item, operation):
        parameters = scope.get("parameters", [])
        if not isinstance(parameters, list):
            raise CompileError("UCP REST parameters must be an array")
        for raw in parameters:
            parameter = await loader.expand(raw, schema_url)
            if not isinstance(parameter, dict):
                raise CompileError("UCP REST parameter must be an object")
            name = parameter.get("name")
            if parameter.get("in") == "header" and isinstance(name, str):
                if not _header_allowed(parameter, name, warnings):
                    continue
                if name.lower() == "ucp-agent":
                    warnings.add(
                        "UCP-Agent must be supplied by the caller; no platform identity or negotiation is generated"
                    )
            result.append(parameter)
    return result


def _header_allowed(parameter: dict[str, Any], name: str, warnings: set[str]) -> bool:
    """Respect existing broker header ownership without removing required request obligations."""
    try:
        validate_header(name, "")
        if name.lower() in _CREDENTIAL_HEADERS:
            raise SecurityViolationError("Signing header requires host authority")
    except SecurityViolationError:
        if parameter.get("required"):
            raise CompileError("UCP required credentials, signing or broker-owned headers are unsupported") from None
        warnings.add("Optional credential, signing and broker-owned headers are not exposed")
        return False
    return True
