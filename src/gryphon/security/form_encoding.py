"""Closed default OpenAPI form bodies with scalar and repeated scalar fields."""

from __future__ import annotations

import math
import re
import secrets
from typing import Any

from gryphon.errors import InputValidationError

_SCALARS = frozenset({"string", "boolean", "integer", "number"})
_MULTIPART_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_MAX_MULTIPART_PARTS = 1024
_MAX_MULTIPART_BYTES = 2 * 1024 * 1024
_BOUNDARY_BYTES = 24


def check_form_schema(schema: dict[str, Any], *, multipart: bool = False) -> None:
    """Reject ambiguous, nullable, nested or open-ended form contracts.

    The compiler closes unspecified additional properties before this check.
    Only default form style with exploded arrays is supported.
    """
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("Form bodies require a closed object schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("Invalid form properties")
    for name, field in properties.items():
        if multipart and (not isinstance(name, str) or not _MULTIPART_NAME.fullmatch(name)):
            raise ValueError("Multipart field names must be bounded ASCII identifiers")
        if not isinstance(field, dict):
            raise ValueError("Invalid form field")
        kind = field.get("type")
        items = field.get("items", {}) if kind == "array" else {}
        if multipart and any(
            node.get("format") in {"binary", "byte"} for node in (field, items) if isinstance(node, dict)
        ):
            raise ValueError("Multipart file and binary schemas are unsupported")
        if kind == "array":
            kind = items.get("type") if isinstance(items, dict) else None
        if not isinstance(kind, str) or kind not in _SCALARS:
            raise ValueError("Form fields require non-null scalars or scalar arrays")


def encode_form(body: Any) -> dict[str, str | list[str]]:
    """Encode scalars without JSON quoting; arrays become repeated form keys."""
    if not isinstance(body, dict) or any(not isinstance(key, str) for key in body):
        raise InputValidationError("Form bodies must be JSON objects")
    return {
        key: [_form_scalar(item) for item in value] if isinstance(value, list) else _form_scalar(value)
        for key, value in body.items()
    }


def encode_multipart(body: Any) -> tuple[str, bytes]:
    """Serialize at most 1024 scalar parts and 2 MiB; never interpret values as files.

    Names are restricted to 1–128 ASCII letters, digits, underscores, dots or
    hyphens. The broker generates the boundary and the matching Content-Type.
    Values are literal UTF-8 text, including paths, quotes and header-like text.
    """
    if not isinstance(body, dict) or len(body) > _MAX_MULTIPART_PARTS:
        raise InputValidationError("Multipart bodies require a bounded object")
    boundary = secrets.token_hex(_BOUNDARY_BYTES)
    closing = f"--{boundary}--\r\n".encode("ascii")
    content = bytearray()
    count = 0
    for name, value in body.items():
        if not isinstance(name, str) or not _MULTIPART_NAME.fullmatch(name):
            raise InputValidationError("Invalid multipart field name")
        values = value if isinstance(value, list) else [value]
        count += len(values)
        if count > _MAX_MULTIPART_PARTS:
            raise InputValidationError("Multipart part count exceeds limit")
        for item in values:
            part = _multipart_part(name, item, boundary)
            if len(content) + len(part) + len(closing) > _MAX_MULTIPART_BYTES:
                raise InputValidationError("Multipart body exceeds byte limit")
            content.extend(part)
    content.extend(closing)
    return f"multipart/form-data; boundary={boundary}", bytes(content)


def _multipart_part(name: str, value: Any, boundary: str) -> bytes:
    """Emit one scalar part without caller-supplied disposition, filename or headers."""
    text = _form_scalar(value)
    if len(text) > _MAX_MULTIPART_BYTES or boundary in text:
        raise InputValidationError("Multipart value exceeds limit or conflicts with boundary")
    try:
        payload = text.encode("utf-8")
    except UnicodeError:
        raise InputValidationError("Multipart text must be valid UTF-8") from None
    if len(payload) > _MAX_MULTIPART_BYTES:
        raise InputValidationError("Multipart value exceeds byte limit")
    header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
    return header + payload + b"\r\n"


def _form_scalar(value: Any) -> str:
    """Reject objects and null instead of silently stringifying unsupported wire values."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or isinstance(value, float) and math.isfinite(value):
        return str(value)
    raise InputValidationError("Form values must be finite non-null JSON scalars")
