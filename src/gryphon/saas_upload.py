"""Validate uploaded specifications without fetching tenant-selected host files or references."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import yaml

from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import InputValidationError
from gryphon.models import SpecDiagnostics, SpecImport, SwaggerSource
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.saas_catalog import validate_uploaded_document

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


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


def _inspect(content: str, config: GryphonConfig, limit: int, name: str, origin: str | None = None) -> SpecImport:
    """Measure the actual parser result, including method-policy exclusions, before publication."""
    document = _document(content, limit)
    if origin is not None:
        _remote_servers(document, origin)
    with TemporaryDirectory(prefix="gryphon-upload-") as directory:
        path = Path(directory) / "spec.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        source = SwaggerSource(name=name, swagger_url=str(path), is_read_only=True)
        parsed = asyncio.run(SwaggerParser(source, max_spec_size_bytes=limit * 2, config=config).parse())
    methods = {"get", "head", "options", "post", "put", "patch", "delete"}
    total = sum(method.lower() in methods for item in document.get("paths", {}).values() for method in item)
    diagnostics = SpecDiagnostics(
        total_operations=total,
        available_operations=len(parsed.endpoints),
        filtered_operations=total - len(parsed.endpoints),
    )
    warnings = []
    if diagnostics.filtered_operations:
        warnings.append(
            "Non-read methods are filtered unless the operator explicitly approves exact read-only POST routes."
        )
    if not parsed.endpoints:
        warnings.append("No callable operations are available under the current read-only policy.")
    return SpecImport(document=document, diagnostics=diagnostics, warnings=warnings)


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
    content: str, config: GryphonConfig, limit: int, name: str, *, origin: str | None = None
) -> SpecImport:
    """Offload bounded validation and return a snapshot with honest operation counts."""
    return await finish_cleanup(asyncio.to_thread(_inspect, content, config, limit, name, origin))


async def validate_upload(content: str, config: GryphonConfig, limit: int) -> dict[str, Any]:
    """Offload bounded parsing and await worker-owned temporary cleanup on cancellation."""
    return await finish_cleanup(asyncio.to_thread(_validate, content, config, limit))
