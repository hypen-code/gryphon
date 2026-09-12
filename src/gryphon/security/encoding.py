"""Closed JSON-schema arguments and conservative OpenAPI wire encoding."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from gryphon.errors import InputValidationError, SecurityViolationError
from gryphon.security.form_encoding import encode_form, encode_multipart
from gryphon.security.policies import validated_url
from gryphon.security.schema import validate_contract, walk_json

if TYPE_CHECKING:
    from gryphon.models import EndpointManifest

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_PROTECTED_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "trailer",
        "te",
        "forwarded",
        "accept-encoding",
        "content-type",
        "x-http-method-override",
        "x-method-override",
        "x-original-url",
        "x-rewrite-url",
    }
)


def validate_arguments(endpoint: EndpointManifest, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional wire omissions and validate closed, bounded strict JSON.

    Args:
        endpoint: Registry-validated endpoint contract.
        arguments: Sandbox object keyed by original OpenAPI wire names.

    Returns:
        An independent JSON object with optional null query/header values omitted.
        Explicit JSON body nulls remain subject to the declared body schema.
    """
    schema = endpoint.input_schema
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise InputValidationError("Endpoint does not declare a closed input schema")
    try:
        for _ in walk_json(arguments):
            pass
        normalized: dict[str, Any] = json.loads(json.dumps(arguments, allow_nan=False))
        if not isinstance(normalized, dict):
            raise ValueError("Arguments must be a JSON object")
        _normalize_wire_nulls(endpoint, normalized)
        validate_contract(normalized, schema)
    except (ValueError, TypeError, RecursionError):
        raise InputValidationError("Arguments or schema are invalid; schemas must be resolved and bounded") from None
    return normalized


def _normalize_wire_nulls(endpoint: EndpointManifest, arguments: dict[str, Any]) -> None:
    """Omit optional scalar wire nulls, retaining JSON body nulls and rejecting ambiguity."""
    required = endpoint.input_schema.get("required", [])
    for param in endpoint.parameters:
        if param.name not in arguments or param.location == "body":
            continue
        value = arguments[param.name]
        if value is None:
            if param.location == "path" or param.required or param.name in required:
                raise InputValidationError("Required or path wire parameters cannot be null")
            if param.location not in {"query", "header"}:
                raise InputValidationError("Null values have no supported HTTP wire encoding")
            del arguments[param.name]
        elif isinstance(value, list) and any(item is None for item in value):
            raise InputValidationError("Null array items have no supported HTTP wire encoding")


def validate_header(name: str, value: str, *, trusted: bool = False) -> None:
    """Reject routing overrides and unsafe header bytes.

    Args:
        name: Declared wire header name.
        value: Encoded scalar header value.
        trusted: Whether this is an administrator-configured extra header.
    """
    lower = name.lower()
    protected = lower in _PROTECTED_HEADERS or lower.startswith(("proxy-", "x-forwarded-"))
    credential = any(part in lower for part in ("auth", "token", "secret", "key", "session", "credential", "csrf"))
    if not _HEADER_NAME.fullmatch(name) or protected or (credential and not trusted):
        raise SecurityViolationError("Request header override is not permitted")
    if any(ord(char) < 32 or ord(char) >= 127 for char in value):
        raise InputValidationError("Request header value is invalid")


def encode_request(
    endpoint: EndpointManifest,
    base_url: str,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, str], Any]:
    """Encode only declared wire inputs, without accepting routing overrides.

    Args:
        endpoint: Trusted endpoint with real parameter metadata.
        base_url: Manifest-owned base URL.
        arguments: Schema-validated JSON object with optional wire nulls omitted.

    Returns:
        URL, noncredential headers, and JSON or encoded form body. The caller tracks
        json_body presence and uses the manifest media type to select transport encoding.
    """
    base = _routing_base(base_url, endpoint.path)
    declared = {param.name for param in endpoint.parameters}
    if endpoint.request_body_schema is not None:
        declared.add("json_body")
    if set(arguments) - declared:
        raise InputValidationError("Undeclared endpoint arguments are not permitted")
    path, query, headers = _encode_parameters(endpoint, arguments)
    if "{" in path or "}" in path:
        raise InputValidationError("Required path argument is missing")
    url = httpx.URL(str(base).rstrip("/") + path)
    if query:
        url = url.copy_with(query=str(httpx.QueryParams(tuple(query))).encode("ascii"))
    body = arguments.get("json_body")
    if endpoint.request_body_media_type == "application/x-www-form-urlencoded":
        headers["Content-Type"] = endpoint.request_body_media_type
        body = encode_form(body) if "json_body" in arguments else None
    elif endpoint.request_body_media_type == "multipart/form-data":
        headers["Content-Type"], body = encode_multipart(arguments.get("json_body", {}))
    return str(url), headers, body


def _encode_parameters(
    endpoint: EndpointManifest,
    arguments: dict[str, Any],
) -> tuple[str, list[tuple[str, str]], dict[str, str]]:
    """Apply scalar/simple-path and default form/exploded-query encodings."""
    path = endpoint.path
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    for param in endpoint.parameters:
        if param.name not in arguments:
            continue
        value = arguments[param.name]
        if param.location == "path":
            path = path.replace("{" + param.name + "}", _path_value(value))
        elif param.location == "query":
            values = value if isinstance(value, list) else [value]
            query.extend((param.name, _scalar(item)) for item in values)
        elif param.location == "header":
            encoded = _scalar(value)
            validate_header(param.name, encoded)
            headers[param.name] = encoded
        elif param.location != "body" or param.name != "json_body":
            raise InputValidationError("Unsupported parameter encoding")
    return path, query, headers


def _routing_base(base_url: str, path: str) -> httpx.URL:
    """Reject ambiguous routing metadata independently of parameter encoding."""
    base = validated_url(base_url)
    if base.query or not path.startswith("/") or path.startswith("//") or any(c in path for c in "\\?#"):
        raise SecurityViolationError("Endpoint routing metadata is invalid")
    if any(part in {".", ".."} for part in path.split("/")) or "%" in path:
        raise SecurityViolationError("Endpoint path is invalid")
    return base


def _scalar(value: Any) -> str:
    """Serialize a JSON scalar; unsupported object-style parameters fail closed."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    raise InputValidationError("Only scalar wire parameters are supported")


def _path_value(value: Any) -> str:
    """Encode a single path segment and prohibit traversal or separator ambiguity."""
    raw = _scalar(value)
    if raw in {"", ".", ".."} or any(char in raw for char in "/\\%?#"):
        raise InputValidationError("Path argument must be a single safe segment")
    return quote(raw, safe="")
