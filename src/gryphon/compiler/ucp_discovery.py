"""Bounded UCP entry-point inference without redirects or HTML tool discovery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from gryphon.compiler.documents import DEFAULT_MAX_DOCUMENT_BYTES
from gryphon.compiler.ucp import profile_to_openapi
from gryphon.compiler.ucp_mcp import tools_to_openapi
from gryphon.compiler.ucp_profile import (
    SHOPPING_SERVICE,
    SUPPORTED_VERSIONS,
    bounded_json,
    capabilities,
    secure_url,
    shopping_binding,
)
from gryphon.errors import CompileError, ExecutionError, UCPImportError
from gryphon.models import SpecImport
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.security.mcp_client import discover_tools
from gryphon.security.network import decode_json
from gryphon.security.policies import validated_url

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.security.network import NetworkClient


def profile_location(location: str) -> tuple[str, bool]:
    """Resolve root/endpoint inputs to exactly one same-origin well-known profile probe."""
    url = validated_url(location)
    endpoint = url.path.rstrip("/").lower().endswith("/mcp")
    if url.path in {"", "/"} or endpoint:
        return str(url.copy_with(path="/.well-known/ucp")), endpoint
    return location, False


async def discover_ucp(
    config: GryphonConfig,
    location: str,
    max_bytes: int,
    client: NetworkClient,
) -> SpecImport:
    """Prefer the real profile's advertised transport; only explicit MCP paths get fallback."""
    limit = min(max_bytes, config.max_spec_size_bytes, DEFAULT_MAX_DOCUMENT_BYTES)
    location = secure_url(location, config)
    profile_url, explicit_endpoint = profile_location(location)
    profile: Any = None
    profile_bytes = 0
    try:
        response = await client.request("GET", profile_url, max_bytes=limit, headers={"Accept": "application/json"})
        profile_bytes = len(response.content)
        profile = await finish_cleanup(asyncio.to_thread(decode_json, response))
    except ExecutionError:
        if not explicit_endpoint:
            raise UCPImportError(
                "UCP profile fetch failed; supply a public HTTPS JSON profile or explicit MCP endpoint"
            ) from None
    if not isinstance(profile, dict) or "ucp" not in profile:
        if not explicit_endpoint:
            raise UCPImportError("UCP entry is not a JSON business profile; HTML pages cannot advertise callable tools")
        return await _mcp(config, location, None, None, limit - profile_bytes)
    await finish_cleanup(asyncio.to_thread(bounded_json, profile, limit))
    try:
        ucp, version, advertised = _profile(profile)
        transport = _transport(ucp, version)
        binding = shopping_binding(ucp, version, set(), transport=transport)
        endpoint = secure_url(binding.get("endpoint"), config)
        if transport == "mcp":
            return await _mcp(config, endpoint, profile_url, (version, advertised), limit - profile_bytes)
        document = await profile_to_openapi(profile, profile_url, config, limit, profile_bytes=profile_bytes)
    except UCPImportError:
        raise
    except CompileError:
        raise UCPImportError("UCP profile has no usable matching shopping binding or supported contract") from None
    return SpecImport(
        document=document,
        resolved_profile_url=profile_url,
        resolved_endpoint=endpoint,
        source_transport="rest",
        warnings=document["x-gryphon-ucp"]["warnings"],
    )


def _profile(profile: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, list[dict[str, Any]]]]:
    """Reject unsupported or malformed profiles before attempting advertised endpoint discovery."""
    ucp = profile.get("ucp")
    if not isinstance(ucp, dict) or not isinstance(ucp.get("version"), str):
        raise CompileError("Invalid UCP profile metadata")
    version = ucp["version"]
    if version not in SUPPORTED_VERSIONS:
        raise CompileError("Unsupported UCP profile version")
    return ucp, version, capabilities(ucp, version)


def _transport(ucp: dict[str, Any], version: str) -> str:
    """Preserve REST preference, selecting MCP only when no matching REST is advertised."""
    services = ucp.get("services")
    if not isinstance(services, dict):
        raise CompileError("UCP services must be an object")
    records = services.get(SHOPPING_SERVICE)
    if version == "2026-01-11" and isinstance(records, dict):
        return "rest" if "rest" in records else "mcp"
    if not isinstance(records, list):
        raise CompileError("UCP shopping bindings must be an array")
    return (
        "rest"
        if any(
            isinstance(record, dict) and record.get("transport") == "rest" and record.get("version") == version
            for record in records
        )
        else "mcp"
    )


async def _mcp(
    config: GryphonConfig,
    endpoint: str,
    profile_url: str | None,
    profile: tuple[str, dict[str, list[dict[str, Any]]]] | None,
    limit: int,
) -> SpecImport:
    """Discover metadata only, retaining actual endpoint authority outside the synthetic document."""
    if limit <= 0:
        raise UCPImportError("UCP profile exhausted the aggregate discovery byte budget")
    try:
        tools = await discover_tools(config, endpoint, limit)
    except ExecutionError:
        raise UCPImportError(
            "MCP metadata discovery failed; check the endpoint supports initialize and tools/list"
        ) from None
    version, advertised = profile if profile is not None else ("unprofiled", None)
    document, bindings = await finish_cleanup(
        asyncio.to_thread(tools_to_openapi, tools, endpoint, version, advertised, limit)
    )
    if profile is None:
        document["x-gryphon-ucp"]["warnings"].append(
            "No UCP profile found; explicit endpoint tools have no advertised capability validation."
        )
    return SpecImport(
        document=document,
        mcp_bindings=bindings,
        resolved_endpoint=endpoint,
        resolved_profile_url=profile_url,
        source_transport="mcp",
        warnings=document["x-gryphon-ucp"]["warnings"],
    )
