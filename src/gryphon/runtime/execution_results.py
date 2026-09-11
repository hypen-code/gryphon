"""Static failure envelopes and policy fingerprints for Gryphon execution."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from gryphon.errors import (
    CacheError,
    CapacityError,
    ConflictError,
    DockerUnavailableError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    InputValidationError,
    SecurityViolationError,
    ServerNotFoundError,
)
from gryphon.models import ExecutionResult
from gryphon.runtime.execution_validation import json_bytes
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.runtime.artifacts import ArtifactStore
    from gryphon.runtime.registry import Registry

logger = get_logger(__name__)
_CACHE_ID_BYTES = 64
_RUN_ID_BYTES = 32


def fingerprint(config: GryphonConfig, registry: Registry) -> str:
    """Bind request identity to the authoritative catalog and execution policy.

    Args:
        config: Trusted sandbox, egress, and write authority settings.
        registry: Current loaded catalog with a deterministic fingerprint.

    Returns:
        Stable SHA-256 identity; a changed policy invalidates retained recipes.
    """
    catalog = registry.fingerprint()
    policy = {
        name: getattr(config, name)
        for name in (
            "sandbox_mode",
            "docker_runtime",
            "docker_image",
            "allowed_domains",
            "allow_private_networks",
            "allow_writes",
            "allowed_write_operations",
            "max_tool_calls",
            "sandbox_memory_bytes",
            "execution_timeout_seconds",
            "max_response_size_bytes",
            "max_output_size_bytes",
            "container_memory_limit",
        )
    }
    policy["allowed_write_operations"] = sorted(set(config.allowed_write_operations))
    return hashlib.sha256(json.dumps([catalog, policy], sort_keys=True).encode()).hexdigest()


async def bound_result(
    result: ExecutionResult,
    owner: str,
    config: GryphonConfig,
    artifacts: ArtifactStore,
) -> ExecutionResult:
    """Bound the complete JSON envelope, preserving oversized inline data as an artifact.

    Args:
        result: Backend result containing strict JSON-only data.
        owner: Trusted namespace used for artifact ownership.
        config: Full-result and inline-output byte limits.
        artifacts: Executor-owned artifact store.

    Returns:
        Detached bounded envelope with space reserved for durable identifiers.
    """
    envelope = result.model_dump()
    envelope["run_id"] = result.run_id or "0" * _RUN_ID_BYTES
    if result.success and config.cache_enabled:
        envelope["cache_id"] = result.cache_id or "0" * _CACHE_ID_BYTES
    encoded = json_bytes(envelope, config.max_response_size_bytes)
    result = result.model_copy(deep=True)
    if len(encoded) > config.max_output_size_bytes:
        if not result.success:
            raise CapacityError("Execution result envelope exceeds output limit")
        artifact = await artifacts.put(result.data, owner)
        result.data = {"summary": "Result stored as a JSON artifact", "size_bytes": artifact.size_bytes}
        result.artifact_id, result.truncated = artifact.id, True
        envelope.update(data=result.data, artifact_id=result.artifact_id, truncated=True)
    json_bytes(envelope, config.max_output_size_bytes)
    return result


def failure(exc: Exception, run_id: str | None = None) -> ExecutionResult:
    """Translate failures to static messages without exception stringification.

    Args:
        exc: Internal exception; its message and traceback are never exposed.
        run_id: Durable receipt identifier, when already admitted.

    Returns:
        Safe public failure with a deterministic machine-readable category.
    """
    kinds: list[tuple[type[Exception], str, str]] = [
        (CapacityError, "capacity", "Execution capacity or resource budget exceeded"),
        (ExecutionTimeoutError, "timeout", "Execution timed out"),
        (TimeoutError, "timeout", "Execution timed out"),
        (SecurityViolationError, "security", "Execution blocked by security policy"),
        (InputValidationError, "validation", "Invalid source, inputs, schema, or JSON result"),
        (ConflictError, "conflict", "Request identity conflicts or cached catalog/profile is stale"),
        (CacheError, "cache", "Execution storage is unavailable or the owned record was not found"),
        (DockerUnavailableError, "sandbox_unavailable", "Configured Docker sandbox is unavailable"),
        (FunctionNotFoundError, "not_found", "Broker capability not found"),
        (ServerNotFoundError, "not_found", "Broker capability not found"),
        (ExecutionError, "execution", "Sandbox execution failed"),
    ]
    kind, message = next(
        ((kind, message) for cls, kind, message in kinds if isinstance(exc, cls)),
        ("internal", "Execution failed safely"),
    )
    logger.warning("execution_failed", error_type=kind)
    return ExecutionResult(success=False, error=message, error_type=kind, run_id=run_id)
