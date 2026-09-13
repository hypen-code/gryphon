"""Bounded JSON contracts and source preparation for Gryphon execution."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import textwrap
from typing import TYPE_CHECKING, Any

from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from gryphon.errors import InputValidationError, SecurityViolationError
from gryphon.security.ast_guard import configured_imports

if TYPE_CHECKING:
    from collections.abc import Iterator

    from gryphon.config import GryphonConfig

_MAX_DEPTH = 32
_MAX_NODES = 100_000
_SCHEMA_MAX_NODES = 1024
_SCHEMA_MAX_DEPTH = 12
_SCHEMA_DIALECTS = frozenset(
    {
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2020-12/schema",
    }
)
_UNBOUNDED_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "$dynamicRef",
        "$recursiveRef",
        "$id",
        "pattern",
        "patternProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentSchemas",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
        "contains",
    }
)
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "dependencies"})
_SCHEMA_CHILDREN = frozenset({"items", "additionalItems", "additionalProperties", "propertyNames", "prefixItems"})


def json_bytes(value: Any, limit: int) -> bytes:
    """Encode strictly JSON-native data under a byte, nesting, and node budget.

    Args:
        value: JSON primitives, string-keyed dictionaries, and lists only.
        limit: Maximum encoded UTF-8 bytes.

    Returns:
        Canonical JSON bytes without lossy conversion or non-finite numbers.
    """
    nodes = 0
    budget = 0

    def visit(item: Any, depth: int) -> None:
        """Reject cycles, deep nesting, foreign objects, and oversized values."""
        nonlocal nodes, budget
        nodes += 1
        budget += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES or budget > limit:
            raise InputValidationError("JSON value exceeds structural or size limits")
        if type(item) is str:
            budget += len(item.encode("utf-8"))
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise InputValidationError("JSON object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) not in (int, float, bool, type(None)):
            raise InputValidationError("Value is not JSON-native")
        elif type(item) is float and not math.isfinite(item):
            raise InputValidationError("JSON numbers must be finite")
        if budget > limit:
            raise InputValidationError("JSON value exceeds size limit")

    try:
        visit(value, 0)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise InputValidationError("Value cannot be encoded as bounded JSON") from exc
    if len(encoded) > limit:
        raise InputValidationError("JSON value exceeds size limit")
    return encoded


def validate_request(
    code: str,
    description: str,
    inputs: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    owner: str,
    key: str | None,
    config: GryphonConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate bounded metadata before source parsing and detach JSON inputs.

    Args:
        code: Original caller source, checked before AST parsing.
        description: Recipe metadata, not logged.
        inputs: Explicit JSON values.
        schema: Optional bounded input contract.
        owner: Trusted authenticated namespace.
        key: Optional immutable owner-scoped request key.
        config: Trusted server limits.

    Returns:
        Detached validated inputs and their contract.
    """
    if not isinstance(code, str) or len(code.encode("utf-8")) > config.max_code_size_bytes:
        raise InputValidationError("Code exceeds size limit or is not a string")
    if not isinstance(description, str) or len(description.encode()) > 4096:
        raise InputValidationError("Invalid execution description")
    if not isinstance(owner, str) or not owner or len(owner) > 1024:
        raise InputValidationError("Invalid execution owner")
    if key is not None and (not isinstance(key, str) or not key or len(key) > 1024):
        raise InputValidationError("Invalid idempotency key")
    return validate_inputs(inputs, schema, config)


def validate_inputs(
    inputs: dict[str, Any] | None, schema: dict[str, Any] | None, config: GryphonConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Snapshot and validate execution inputs without network schema resolution.

    Args:
        inputs: Explicit input object, never substituted into source text.
        schema: Optional bounded JSON Schema; references and regexes are disabled.
        config: Server-owned limits.

    Returns:
        Detached input and schema dictionaries for immutable execution identity.
    """
    if inputs is not None and type(inputs) is not dict:
        raise InputValidationError("inputs must be a JSON object")
    if schema is not None and type(schema) is not dict:
        raise InputValidationError("input_schema must be a JSON object")
    values = json.loads(json_bytes(inputs if inputs is not None else {}, config.max_response_size_bytes))
    contract = json.loads(json_bytes(schema if schema is not None else {}, config.max_code_size_bytes))
    _check_schema_budget(contract)
    _check_schema_directives(contract)
    try:
        validator = validators.validator_for(contract)
        validator.check_schema(contract)
        validator(contract).validate(values)
    except (SchemaError, ValidationError, ValueError, RecursionError) as exc:
        raise InputValidationError("Inputs do not match a valid bounded input_schema") from exc
    return values, contract


def _check_schema_budget(schema: dict[str, Any]) -> None:
    """Count all schema data including annotations without interpreting property names."""
    pending: list[tuple[Any, int]] = [(schema, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _SCHEMA_MAX_NODES or depth > _SCHEMA_MAX_DEPTH:
            raise InputValidationError("input_schema exceeds structural limits")
        if isinstance(item, dict):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)


def _check_schema_directives(schema: dict[str, Any]) -> None:
    """Reject costly directives only at schema positions, never inside literal data."""
    pending: list[Any] = [schema]
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        if _UNBOUNDED_SCHEMA_KEYS.intersection(item):
            raise InputValidationError("Schema references, regexes, and combinators are not supported")
        dialect = item.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        if not isinstance(dialect, str) or dialect not in _SCHEMA_DIALECTS:
            raise InputValidationError("Unsupported input_schema dialect")
        for name, child in item.items():
            if name in _SCHEMA_MAPS and isinstance(child, dict):
                pending.extend(child.values())
            elif name in _SCHEMA_CHILDREN:
                pending.extend(child if isinstance(child, list) else [child])


def effective_imports(config: GryphonConfig) -> frozenset[str]:
    """Resolve current import authority without ever granting restricted VM imports.

    Args:
        config: Trusted settings, revalidated even after in-memory mutation.

    Returns:
        The configured Docker subset or the empty restricted import profile.

    Raises:
        SecurityViolationError: An import setting or sandbox profile is invalid.
    """
    selected = configured_imports(config.sandbox_allowed_imports)
    if config.sandbox_mode not in ("restricted", "docker"):
        raise SecurityViolationError("Unknown execution profile")
    return selected if config.sandbox_mode == "docker" else frozenset()


def prepare_source(code: str, profile: str) -> str:
    """Give final expressions, result assignments, and explicit returns one execution.

    Args:
        code: Source already scanned by ASTGuard.
        profile: Restricted VM or offline Docker profile.

    Returns:
        Source with an explicit final result expression; main is never auto-called.
    """
    if profile not in ("restricted", "docker"):
        raise SecurityViolationError("Unknown execution profile")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise InputValidationError("Invalid Python syntax") from exc
    for node in ast.walk(tree):
        if profile == "restricted" and isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SecurityViolationError("Imports are disabled in the restricted profile")
        if profile == "docker" and isinstance(node, ast.Name) and node.id == "call_tool":
            raise SecurityViolationError("Docker is offline compute; call_tool requires the restricted profile")
    statements = list(_top_level_nodes(tree))
    if any(isinstance(node, ast.Return) for node in statements):
        return "async def _gryphon_execute():\n" + textwrap.indent(code, "    ") + "\nawait _gryphon_execute()"
    if not tree.body:
        raise InputValidationError("Code must provide a result assignment, final expression, or return")
    last = tree.body[-1]
    if isinstance(last, ast.Expr) and not (
        isinstance(last.value, ast.Call) and isinstance(last.value.func, ast.Name) and last.value.func.id == "print"
    ):
        return code
    if any(
        isinstance(node, ast.Name) and node.id == "result" and isinstance(node.ctx, ast.Store) for node in statements
    ):
        return code + "\nresult"
    raise InputValidationError(
        "Code must provide a result assignment, final expression, or return; call main explicitly"
    )


def _top_level_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk control flow while excluding nested function and class bodies."""
    yield tree
    for child in ast.iter_child_nodes(tree):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _top_level_nodes(child)


def request_digest(code: str, inputs: dict[str, Any], schema: dict[str, Any], fingerprint: str, profile: str) -> str:
    """Hash the complete immutable request, never merely normalized source.

    Args:
        code: Exact original source.
        inputs: Detached validated inputs.
        schema: Detached validated contract.
        fingerprint: Catalog and execution policy fingerprint.
        profile: Explicit sandbox profile.

    Returns:
        SHA-256 identity for owner-scoped durable idempotency.
    """
    payload = json.dumps(
        [code, inputs, schema, fingerprint, profile], sort_keys=True, ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()
