"""Validate uploaded specifications without fetching tenant-selected host files or references."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import yaml

from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.ucp_mcp import filter_document
from gryphon.errors import InputValidationError
from gryphon.models import SpecDiagnostics, SpecImport, SwaggerSource
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.saas_catalog import validate_uploaded_document

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import ReadOnlyPostOperation, SaaSSpec, ServerSpec


def _document(content: str, limit: int) -> dict[str, Any]:
    """Reject non-JSON YAML, reference fetches, cycles and oversized specification trees."""
    if len(content.encode()) > limit:
        raise InputValidationError("Specification too large")
    try:
        document = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError, ValueError):
        raise InputValidationError("Invalid specification") from None
    if not isinstance(document, dict):
        raise InputValidationError("Specification must be an object")
    json_bytes(document, limit)
    validate_uploaded_document(document, limit)
    return document


def _validate(content: str, config: GryphonConfig, limit: int) -> dict[str, Any]:
    """Compile the uploaded bytes in an owned temporary directory using the normal parser."""
    return _inspect(content, config, limit, "uploaded").document


def _inspect(
    content: str,
    config: GryphonConfig,
    limit: int,
    name: str,
    origin: str | None = None,
    read_only_filter: bool = True,
    previous: SaaSSpec | None = None,
) -> SpecImport:
    """Measure the actual parser result, including method-policy exclusions, before publication."""
    document = _document(content, limit)
    if origin is not None:
        _remote_servers(document, origin)
    approvals = _retained_approvals(document, previous)
    scoped = config.model_copy(
        update={"allowed_read_only_post_operations": [*config.allowed_read_only_post_operations, *approvals]}
    )
    parsed = parse_uploaded_document(document, scoped, limit, name, read_only_filter)
    methods = {"get", "head", "options", "post", "put", "patch", "delete"}
    total = sum(method.lower() in methods for item in document.get("paths", {}).values() for method in item)
    diagnostics = SpecDiagnostics(
        total_operations=total,
        available_operations=len(parsed.endpoints),
        filtered_operations=total - len(parsed.endpoints),
    )
    warnings = []
    if diagnostics.filtered_operations:
        warnings.append("Non-read HTTP methods are hidden by the read-only filter; uncheck it to include POSTs.")
    if not read_only_filter:
        warnings.append(
            "Included POST operations execute automatically and may have side effects; "
            "other method policies still apply."
            if config.allow_catalog_posts
            else "All supported operations are included in discovery; execution permissions are unchanged."
        )
    if not parsed.endpoints:
        warnings.append("No operations are included in this catalog.")
    return SpecImport(
        document=document,
        read_only_filter=read_only_filter,
        approved_post_reads=approvals,
        diagnostics=diagnostics,
        warnings=warnings,
    )


def _retained_approvals(document: dict[str, Any], previous: SaaSSpec | None) -> list[ReadOnlyPostOperation]:
    """Require re-review after any canonical document change, not just route changes."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if previous is None or hashlib.sha256(encoded).hexdigest() != previous.sha256:
        return []
    return list(previous.approved_post_reads)


def parse_uploaded_document(
    document: dict[str, Any],
    config: GryphonConfig,
    limit: int,
    name: str,
    read_only_filter: bool,
) -> ServerSpec:
    """Parse only validated saved bytes in a worker-owned temporary file, without remote references."""
    canonical = validate_uploaded_document(document, limit)
    with TemporaryDirectory(prefix="gryphon-upload-") as directory:
        path = Path(directory) / "spec.json"
        path.write_text(canonical, encoding="utf-8")
        source = SwaggerSource(name=name, swagger_url=str(path), is_read_only=read_only_filter)
        return asyncio.run(SwaggerParser(source, max_spec_size_bytes=limit * 2, config=config).parse())


def _remote_servers(document: dict[str, Any], origin: str) -> None:
    """Resolve OpenAPI relative server URLs against their fetched document before snapshotting."""
    if "openapi" not in document:
        return
    if not document.get("servers"):
        document["servers"] = [{"url": "/"}]
    contexts = [document]
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        raise InputValidationError("Invalid paths")
    for item in paths.values():
        if isinstance(item, dict):
            contexts.append(item)
            contexts.extend(
                value
                for method, value in item.items()
                if method.lower() in {"get", "head", "options", "post", "put", "patch", "delete"}
                and isinstance(value, dict)
            )
    for context in contexts:
        servers = context.get("servers", [])
        if not isinstance(servers, list):
            raise InputValidationError("Invalid servers")
        for server in servers:
            if not isinstance(server, dict) or not isinstance(server.get("url"), str):
                raise InputValidationError("Invalid server URL")
            server["url"] = urljoin(origin, server["url"])


async def inspect_upload(
    content: str,
    config: GryphonConfig,
    limit: int,
    name: str,
    *,
    origin: str | None = None,
    read_only_filter: bool = True,
    previous: SaaSSpec | None = None,
) -> SpecImport:
    """Offload bounded validation and return a snapshot with honest operation counts."""
    if type(read_only_filter) is not bool:
        raise InputValidationError("Read-only filter must be a boolean")
    return await finish_cleanup(
        asyncio.to_thread(_inspect, content, config, limit, name, origin, read_only_filter, previous)
    )


def _inspect_mcp(
    snapshot: SpecImport,
    config: GryphonConfig,
    limit: int,
    name: str,
    read_only_filter: bool,
) -> SpecImport:
    """Validate trusted discovered bindings and keep the complete supported snapshot for refiltering."""
    validate_uploaded_document(snapshot.document, limit)
    selected = filter_document(snapshot.document, snapshot.mcp_bindings, read_only_filter)
    parsed = parse_uploaded_document(selected, config, limit, name, False)
    metadata = snapshot.document.get("x-gryphon-ucp", {})
    supported = len(snapshot.document["paths"])
    diagnostics = SpecDiagnostics(
        total_operations=metadata.get("total_operations", supported),
        available_operations=len(parsed.endpoints),
        filtered_operations=supported - len(parsed.endpoints),
        unsupported_operations=metadata.get("unsupported_operations", 0),
    )
    warnings = set(metadata.get("warnings", []))
    if diagnostics.filtered_operations:
        warnings.add("Non-read MCP tools are filtered using known protocol names, not readOnlyHint annotations.")
    if not read_only_filter:
        warnings.add("Included MCP tools execute through their bound endpoints and may have side effects.")
    if not parsed.endpoints:
        warnings.add("No operations are included in this catalog.")
    result = SpecImport.model_validate(snapshot.model_dump())
    result.diagnostics, result.warnings, result.read_only_filter = diagnostics, sorted(warnings), read_only_filter
    return result


async def inspect_mcp(
    snapshot: SpecImport,
    config: GryphonConfig,
    limit: int,
    name: str,
    read_only_filter: bool,
) -> SpecImport:
    """Accept host-discovered control metadata only, never ordinary uploaded OpenAPI extensions."""
    if type(read_only_filter) is not bool:
        raise InputValidationError("Read-only filter must be a boolean")
    return await finish_cleanup(asyncio.to_thread(_inspect_mcp, snapshot, config, limit, name, read_only_filter))


async def validate_upload(content: str, config: GryphonConfig, limit: int) -> dict[str, Any]:
    """Offload bounded parsing and await worker-owned temporary cleanup on cancellation."""
    return await finish_cleanup(asyncio.to_thread(_validate, content, config, limit))
