"""Validate the published January, April and August 2026 UCP discovery shapes."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from gryphon.compiler.schemas import validate_document_tree
from gryphon.errors import CompileError
from gryphon.security.policies import check_domain_allowed, validated_url

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

SUPPORTED_VERSIONS = frozenset({"2026-01-11", "2026-01-23", "2026-04-08", "2026-08-25"})
SHOPPING_SERVICE = "dev.ucp.shopping"
_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,15}\Z")
_MAX_NAME_LENGTH = 200


def bounded_json(document: Any, max_bytes: int) -> int:
    """Validate finite bounded JSON without interpreting interpolation or aliases."""
    validate_document_tree(document)
    size = 0
    try:
        encoder = json.JSONEncoder(allow_nan=False, sort_keys=True, separators=(",", ":"))
        for chunk in encoder.iterencode(document):
            size += len(chunk.encode())
            if size > max_bytes or "${" in chunk:
                raise CompileError("UCP document exceeds byte limit or contains interpolation")
    except (ValueError, TypeError, RecursionError):
        raise CompileError("UCP document must be bounded JSON") from None
    return size


def secure_url(value: Any, config: GryphonConfig) -> str:
    """Require HTTPS, exact configured domain policy and no URL-carried credentials."""
    if not isinstance(value, str):
        raise CompileError("UCP URL must be an absolute HTTPS URL")
    url = validated_url(value)
    if url.scheme != "https" or url.query or any(char in value for char in "{}"):
        raise CompileError("UCP URLs require HTTPS without queries or templates")
    check_domain_allowed(str(url), config.allowed_domains)
    return str(url)


def origin(url: str) -> tuple[str, str, int]:
    """Compare canonical scheme, hostname and effective port, never suffix matches."""
    parsed = validated_url(url)
    return parsed.scheme, parsed.host, parsed.port or 443


def _name(value: Any) -> str:
    """Bound untrusted reverse-domain capability labels before exposing metadata."""
    if not isinstance(value, str) or len(value) > _MAX_NAME_LENGTH or not _NAME.fullmatch(value):
        raise CompileError("UCP capability or service name is invalid")
    return value


def _records(value: Any) -> list[dict[str, Any]]:
    """Require nonempty record arrays without coercing malformed advertisements."""
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise CompileError("UCP advertisement must contain a nonempty record array")
    return value


def capabilities(ucp: dict[str, Any], version: str) -> dict[str, list[dict[str, Any]]]:
    """Normalize legacy named arrays and newer name-keyed arrays without duplicate models."""
    raw = ucp.get("capabilities")
    result: dict[str, list[dict[str, Any]]] = {}
    if version == "2026-01-11":
        for record in _records(raw):
            name = _name(record.get("name"))
            if name in result:
                raise CompileError("UCP capability advertisement is ambiguous")
            result[name] = [record]
    elif isinstance(raw, dict) and raw:
        result = {_name(name): _records(records) for name, records in raw.items()}
    else:
        raise CompileError("UCP capabilities must be a nonempty name-keyed mapping")
    for records in result.values():
        versions = [record.get("version") for record in records]
        if any(not isinstance(item, str) for item in versions) or len(set(versions)) != len(versions):
            raise CompileError("UCP capability versions are missing or ambiguous")
    return dict(sorted(result.items()))


def shopping_binding(
    ucp: dict[str, Any], version: str, warnings: set[str], *, transport: str = "rest"
) -> dict[str, Any]:
    """Select exactly one matching binding without guessing dispatch from schema URLs."""
    services = ucp.get("services")
    if not isinstance(services, dict):
        raise CompileError("UCP business profile requires services")
    selected: list[dict[str, Any]] = []
    for name, raw in sorted(services.items()):
        _name(name)
        if name != SHOPPING_SERVICE:
            warnings.add(f"Unsupported service: {name}")
            continue
        records = _legacy_bindings(raw) if version == "2026-01-11" else _records(raw)
        for record in records:
            if record.get("transport") != transport:
                warnings.add(
                    "Non-REST transports are not imported"
                    if transport == "rest"
                    else "Other transports are not imported"
                )
            elif record.get("version") != version:
                warnings.add("Bindings for other protocol versions are not imported")
            else:
                selected.append(record)
    if len(selected) != 1:
        raise CompileError("UCP requires exactly one matching shopping transport binding")
    return selected[0]


def _legacy_bindings(raw: Any) -> list[dict[str, Any]]:
    """Convert the original nested transport shape without inventing schema URLs."""
    if not isinstance(raw, dict):
        raise CompileError("UCP legacy service must be an object")
    result = []
    for transport in ("rest", "mcp", "a2a", "embedded"):
        if transport in raw:
            binding = raw[transport]
            if not isinstance(binding, dict):
                raise CompileError("UCP transport binding must be an object")
            result.append({**binding, "transport": transport, "version": raw.get("version")})
    return result
