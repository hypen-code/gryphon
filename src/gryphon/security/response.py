"""Bounded output contracts and fail-closed detection of reflected host credentials."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from gryphon.errors import ExecutionError
from gryphon.security.schema import validate_contract, walk_json

_MAX_SECRET_VALUES = 128
_MAX_SECRET_BYTES = 65536


def validate_response(value: Any, schema: dict[str, Any], headers: dict[str, str]) -> Any:
    """Return unchanged validated JSON, never raw reflected authentication material.

    Args:
        value: Already size-bounded parsed upstream JSON.
        schema: Normalized output contract; empty means bounded JSON only.
        headers: Host-resolved credential headers, never sandbox supplied.

    Returns:
        The original JSON value when safe, without field-name-based redaction.

    Raises:
        ExecutionError: On schema mismatch, excessive structure, or credential reflection.
    """
    try:
        validate_contract(value, schema)
    except (ValueError, RecursionError):
        raise ExecutionError("Upstream response violated its declared schema") from None
    secrets = _secret_values(headers)
    try:
        for item in walk_json(value):
            if isinstance(item, str) and any(secret in item for secret in secrets):
                raise ExecutionError("Upstream response contained credential material")
    except ValueError:
        raise ExecutionError("Upstream response exceeded structural limits") from None
    return value


def _secret_values(headers: dict[str, str]) -> set[str]:
    """Extract only known sensitive values, not benign header names or payload keys."""
    secrets: set[str] = set()
    for name, value in headers.items():
        lower = name.lower()
        sensitive = any(
            part in lower for part in ("auth", "cookie", "token", "secret", "key", "session", "credential", "csrf")
        )
        if not value or not sensitive:
            continue
        secrets.add(value)
        if lower == "authorization":
            scheme, _, token = value.partition(" ")
            if token:
                secrets.add(token)
                if scheme.lower() == "basic":
                    secrets.update(_basic_secrets(token))
        elif lower == "cookie":
            secrets.update(part.partition("=")[2].strip() for part in value.split(";") if "=" in part)
    secrets.discard("")
    if len(secrets) > _MAX_SECRET_VALUES or sum(len(secret) for secret in secrets) > _MAX_SECRET_BYTES:
        raise ExecutionError("Authentication material exceeds supported limits")
    return secrets


def _basic_secrets(token: str) -> set[str]:
    """Recognize a reflected Basic credential or password without exposing either."""
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeError):
        return set()
    return {decoded, decoded.partition(":")[2]} - {""}
