"""Strict JSON context budgets, lifecycle ownership, and safe MCP boundary adapters."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, TypeAdapter

from gryphon.errors import (
    CacheError,
    CapacityError,
    ConflictError,
    DockerUnavailableError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    InputValidationError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.registry import Registry
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastmcp import FastMCP

    from gryphon.config import GryphonConfig
    from gryphon.models import RunRecord
    from gryphon.runtime.artifacts import ArtifactStore

logger = get_logger(__name__)
MAX_CONTEXT_BYTES = 16384
MAX_EXECUTION_BYTES = 65536
MAX_PAGE_SIZE = 100
_JSON_OBJECT = TypeAdapter(dict[str, Any])
_ERROR_TYPES: tuple[tuple[type[Exception], str], ...] = (
    (SecurityViolationError, "security"),
    (InputValidationError, "validation"),
    (ExecutionTimeoutError, "timeout"),
    (CapacityError, "capacity"),
    (ConflictError, "conflict"),
    (LintError, "lint"),
    (DockerUnavailableError, "sandbox_unavailable"),
    (ExecutionError, "execution"),
    (CacheError, "cache"),
    (ServerNotFoundError, "server_not_found"),
    (FunctionNotFoundError, "function_not_found"),
)
_SAFE_ERROR_TYPES = frozenset(kind for _, kind in _ERROR_TYPES) | {
    "internal",
    "cache_miss",
    "not_found",
    "cancelled",
    "interrupted",
    "context_limit",
}
_ENVELOPE_KEYS = frozenset(
    {
        "success",
        "error",
        "error_type",
        "cache_id",
        "run_id",
        "artifact_id",
        "id",
        "status",
        "registry_fingerprint",
        "truncated",
        "next_cursor",
        "total",
        "offset",
        "next_offset",
    }
)


class ServerDependencies:
    """Track explicitly owned initialization without restarting CLI-managed objects."""

    def __init__(
        self, config: GryphonConfig, registry: Registry | None, cache: CacheStore | None, executor: CodeExecutor | None
    ) -> None:
        """Construct missing dependencies; defer all initialization to the lifespan."""
        self.own_registry, self.own_cache, self.own_executor = registry is None, cache is None, executor is None
        # Initialise only owned registry data before serving requests; instructions
        # never depend on reading skills content or compiled host modules.
        self.registry = registry if registry is not None else Registry(config.compiled_output_dir)
        self.cache = (
            cache
            if cache is not None
            else CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
        )
        self.executor = executor if executor is not None else CodeExecutor(config, self.cache, self.registry)

    @asynccontextmanager
    async def lifespan(self, mcp: FastMCP) -> AsyncIterator[None]:
        """Initialize and close only dependencies constructed by this server.

        Args:
            mcp: FastMCP instance invoking the lifespan.

        Yields:
            Control while the server is accepting requests.
        """
        async with AsyncExitStack() as stack:
            if self.own_registry:
                await asyncio.to_thread(self.registry.load)
            if self.own_cache:
                stack.push_async_callback(self.cache.close)
                await self.cache.initialize()
            if self.own_executor:
                stack.push_async_callback(self.executor.shutdown)
                await self.executor.startup()
            logger.info("gryphon_server_initialized", name=mcp.name, owned_executor=self.own_executor)
            yield


def json_bytes(value: Any) -> bytes:
    """Serialize deterministic, strict JSON and measure real UTF-8 bytes.

    Args:
        value: JSON-compatible data; non-finite numbers are rejected.

    Returns:
        Canonical JSON bytes, including escaped control characters and keys.
    """
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def safe_error(error: Exception | str) -> dict[str, Any]:
    """Return an error category without exception messages, code, or credentials.

    Args:
        error: Exception to classify or an internal category name.

    Returns:
        A small structured failure suitable for an MCP response.
    """
    kind = (
        error
        if isinstance(error, str)
        else next((kind for cls, kind in _ERROR_TYPES if isinstance(error, cls)), "internal")
    )
    kind = kind if kind in _SAFE_ERROR_TYPES else "internal"
    logger.warning("tool_operation_failed", error_type=kind)
    return {"success": False, "error": "Request failed; check the arguments and server policy.", "error_type": kind}


def trusted_owner() -> str:
    """Resolve ownership exclusively from verified FastMCP authentication.

    Returns:
        Verified client/subject identity, or the local stdio namespace.

    Raises:
        SecurityViolationError: If an authenticated token has no usable identity.
    """
    token = get_access_token()
    if token is None:
        return "local"
    owner = token.client_id or token.subject or token.claims.get("sub")
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise SecurityViolationError("Authentication identity is invalid")
    return owner


def bounded_json(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """Bound a complete JSON object without returning partial schemas or invalid JSON.

    Args:
        payload: Response data; never mutated by this function.
        budget: Maximum serialized byte count, at least 256.

    Returns:
        Original data or an explicit context-limit envelope retaining small handles.
    """
    if budget < 256:
        raise ValueError("JSON budgets must allow a minimal response envelope")
    encoded = json_bytes(payload)
    if len(encoded) <= budget:
        return _JSON_OBJECT.validate_json(encoded)
    # Keep explicit next-step handles so the LLM can retrieve an artifact or reuse
    # run_cached_code for the next similar request instead of
    # calling execute_code again.
    result = {key: value for key, value in payload.items() if key in _ENVELOPE_KEYS and len(json_bytes(value)) < 256}
    result.update(truncated=True)
    result.setdefault("error", "Response exceeds context budget; narrow the request.")
    result.setdefault("error_type", "context_limit")
    if len(json_bytes(result)) > budget:
        result = {"truncated": True, "error": "Response exceeds context budget", "error_type": "context_limit"}
    return _JSON_OBJECT.validate_json(json_bytes(result))


def bounded_text(payload: dict[str, Any], field: str, budget: int) -> dict[str, Any]:
    """Fit untrusted text using exact serialized-byte checks, never token estimates.

    Args:
        payload: Metadata and a string field to clip.
        field: Name of the only field that may be shortened.
        budget: Maximum serialized JSON size.

    Returns:
        Bounded text plus an explicit truncation marker.
    """
    if len(json_bytes(payload)) <= budget:
        return bounded_json(payload, budget)
    result = {**payload, "truncated": True}
    text = str(result[field])
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        result[field] = text[:middle]
        if len(json_bytes(result)) <= budget:
            low = middle
        else:
            high = middle - 1
    result[field] = text[:low]
    return bounded_json(result, budget)


def _small_identity(item: dict[str, Any]) -> dict[str, Any]:
    """Retain only short identifiers when a complete schema cannot fit."""
    return {
        key: value
        for key, value in item.items()
        if key in {"name", "id", "server_name", "function_name"} and len(json_bytes(value)) < 128
    }


def bounded_page(
    field: str,
    items: list[dict[str, Any]],
    fingerprint: str,
    budget: int,
    *,
    cursor: int = 0,
    total: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Build a deterministic page without cutting function schemas in half.

    Args:
        field: Response list field.
        items: Ordered candidates starting at cursor, including optional lookahead.
        fingerprint: Registry identity for detecting stale discovery.
        budget: Maximum JSON bytes.
        cursor: Absolute start position in the source collection.
        total: Known source size, or a lower bound inferred from candidates.
        limit: Maximum items to return.

    Returns:
        A page with a truncation marker and the next unconsumed cursor.
    """
    total = total if total is not None else cursor + len(items)
    result: dict[str, Any] = {
        field: [],
        "registry_fingerprint": fingerprint,
        "truncated": False,
        "next_cursor": None,
        "total": total,
    }
    for item in items[:limit]:
        candidate = {
            **result,
            field: [*result[field], item],
            "truncated": True,
            "next_cursor": cursor + len(result[field]) + 1,
        }
        if len(json_bytes(candidate)) > budget:
            if not result[field]:
                identity = _small_identity(item)
                result[field].append({**identity, "error_type": "context_limit", "truncated": True})
            break
        result[field].append(item)
    consumed = cursor + len(result[field])
    result["truncated"] = consumed < total or any(item.get("truncated", False) for item in result[field])
    result["next_cursor"] = consumed if consumed < total else None
    return bounded_json(result, budget)


def validate_page(cursor: int, limit: int) -> None:
    """Validate bounded discovery arguments.

    Args:
        cursor: Nonnegative integer position.
        limit: Page size from one through the fixed maximum.

    Raises:
        InputValidationError: If an argument is outside the supported range.
    """
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InputValidationError("Invalid page bounds")


def public_result(result: BaseModel) -> dict[str, Any]:
    """Serialize model-backed execution receipts without internal authority or diagnostics.

    Args:
        result: ExecutionResult or RunRecord from the execution engine.

    Returns:
        Public JSON data with sanitized failure details and a reuse hint.
    """
    dump = result.model_dump(mode="json", exclude_none=True, exclude={"owner", "request_hash", "traceback"})
    if "result" in dump and isinstance(dump["result"], dict):
        nested = getattr(result, "result", None)
        if isinstance(nested, BaseModel):
            dump["result"] = public_result(nested)
    if dump.get("success") is False:
        dump.update(safe_error(dump.get("error_type") or "execution"))
        dump.pop("prints", None)
        dump.pop("data", None)
    if dump.get("success") and dump.get("cache_id"):
        dump["next"] = {"tool": "run_cached_code", "cache_id": dump["cache_id"], "params": "structured inputs"}
    return dump


async def bounded_receipt(record: RunRecord, artifacts: ArtifactStore, owner: str, budget: int) -> dict[str, Any]:
    """Keep completed run data retrievable within the smaller discovery budget.

    Args:
        record: Owner-scoped ledger snapshot; never mutated or written back.
        artifacts: Executor-owned store for data exceeding the receipt budget.
        owner: Verified authentication identity, never a tool argument.
        budget: Maximum serialized receipt bytes.

    Returns:
        Sanitized receipt with inline data or an owner-scoped artifact reference.
        Missing ownership returns the same failure as an unknown run identifier.
    """
    if record.owner != owner:
        return safe_error("not_found")
    payload = public_result(record)
    if len(json_bytes(payload)) <= budget:
        return payload
    result = payload.get("result")
    if isinstance(result, dict) and result.get("success"):
        if not result.get("artifact_id"):
            artifact = await artifacts.put(result.get("data"), owner=owner)
            result["artifact_id"] = artifact.id
        result["data"] = {"summary": "Result stored as a JSON artifact"}
        result["truncated"] = True
    return bounded_json(payload, budget)


def guard_tool(
    function: Callable[..., Awaitable[dict[str, Any]]],
    budget: int,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Apply safe failures and strict JSON bounds while preserving MCP input signatures.

    Args:
        function: Asynchronous tool handler.
        budget: Maximum serialized response size.

    Returns:
        A wrapped handler exposing the original Pydantic-derived signature.
    """

    @wraps(function)
    async def guarded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Return bounded structured data, including for failed tool operations."""
        try:
            logger.info("tool_called", tool=function.__name__, input_bytes=len(json_bytes([args, kwargs])))
            return bounded_json(await function(*args, **kwargs), budget)
        except Exception as exc:
            return safe_error(exc)

    return guarded
