"""Bounded, nonrecursive JSON-schema policy shared by input and output validation."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_SCHEMA_NODES = 8192
_MAX_SCHEMA_DEPTH = 32
_MAX_DATA_NODES = 100000
_MAX_DATA_DEPTH = 64
_MAX_VALIDATION_WORK = 4000000
_MAX_ENUM_ITEMS = 64
_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "title",
        "description",
        "default",
        "examples",
        "example",
        "format",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)


def walk_json(value: Any, max_nodes: int = _MAX_DATA_NODES, max_depth: int = _MAX_DATA_DEPTH) -> Iterator[Any]:
    """Walk finite JSON with explicit size/depth bounds, including object keys.

    Args:
        value: JSON-compatible value.
        max_nodes: Maximum total primitive/container/key count.
        max_depth: Maximum nesting depth.

    Yields:
        Containers and primitive values, without recursion.

    Raises:
        ValueError: If the value is non-JSON or exceeds structural bounds.
    """
    stack = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited + len(stack) > max_nodes or depth > max_depth:
            raise ValueError("JSON structure exceeds supported limits")
        yield item
        if isinstance(item, dict):
            if visited + len(stack) + 2 * len(item) > max_nodes or any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object exceeds supported limits")
            stack.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif isinstance(item, list):
            if visited + len(stack) + len(item) > max_nodes:
                raise ValueError("JSON array exceeds supported limits")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Non-finite JSON number")
        elif item is not None and not isinstance(item, str | int | bool):
            raise ValueError("Unsupported JSON value")


def check_schema(schema: dict[str, Any]) -> int:
    """Reject references, regexes, composition and other unbounded schema semantics.

    Args:
        schema: A normalized, local-only supported JSON Schema.

    Returns:
        Bounded schema node count for validation-work admission.
    """
    count = sum(1 for _ in walk_json(schema, _MAX_SCHEMA_NODES, _MAX_SCHEMA_DEPTH))
    pending = [schema]
    while pending:
        node = pending.pop()
        if set(node) - _ALLOWED_KEYWORDS:
            raise ValueError("Unsupported JSON schema keyword")
        _check_scalar_choices(node)
        properties = node.get("properties", {})
        if not isinstance(properties, dict) or any(not isinstance(child, dict) for child in properties.values()):
            raise ValueError("Invalid schema properties")
        pending.extend(properties.values())
        for name in ("items", "additionalProperties"):
            child = node.get(name)
            if isinstance(child, dict):
                pending.append(child)
            elif child is not None and not isinstance(child, bool):
                raise ValueError("Invalid child schema")
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise ValueError("Invalid JSON schema") from None
    return count


def _check_scalar_choices(schema: dict[str, Any]) -> None:
    """Bound equality checks to small scalar enums and constants."""
    if "const" in schema and isinstance(schema["const"], dict | list):
        raise ValueError("Complex schema constants are unsupported")
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or len(values) > _MAX_ENUM_ITEMS:
            raise ValueError("Schema enum exceeds supported limits")
        if any(isinstance(value, dict | list) for value in values):
            raise ValueError("Complex schema enums are unsupported")


def validate_contract(value: Any, schema: dict[str, Any]) -> None:
    """Apply the shared bounded contract without reflecting values or schema text.

    Args:
        value: Strict JSON input or output value.
        schema: Supported normalized schema; an empty schema permits any bounded JSON.
    """
    data_nodes = sum(1 for _ in walk_json(value))
    if not schema:
        return
    schema_nodes = check_schema(schema)
    if data_nodes * schema_nodes > _MAX_VALIDATION_WORK:
        raise ValueError("JSON schema validation work exceeds supported limits")
    try:
        Draft202012Validator(schema).validate(value)
    except (ValidationError, SchemaError, ValueError, TypeError, RecursionError, ArithmeticError):
        raise ValueError("JSON value does not match declared schema") from None
