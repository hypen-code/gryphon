"""Trusted offline artifact projection preparation within executor-owned admission."""

from __future__ import annotations

import ast
import asyncio
from typing import TYPE_CHECKING, Any

from gryphon.errors import ASTViolationError, ExecutionError, SecurityViolationError
from gryphon.runtime.execution_validation import json_bytes
from gryphon.runtime.sandboxes import RestrictedSandbox
from gryphon.security.ast_guard import ASTGuard

if TYPE_CHECKING:
    from gryphon.models import ExecutionResult, ExecutionScope, RunRecord
    from gryphon.runtime.executor import CodeExecutor


def guard_projection(code: str) -> None:
    """Reject imports and capability references, including aliases, before VM entry."""
    ASTGuard().validate(code, allowed_modules=frozenset())
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Name) and node.id == "call_tool":
            raise ASTViolationError("blocked_call", node.lineno)


def projection_profile(executor: CodeExecutor, code: str, owner: str, artifact_id: str) -> str:
    """Validate the offline profile and bind stored artifact identity to request identity."""
    executor.artifacts.validate_id(artifact_id)
    if executor._config.sandbox_mode != "restricted":
        raise SecurityViolationError("Artifact projection requires the restricted profile")
    guard_projection(code)
    return f"projection:{owner}:{artifact_id}"


async def await_record(executor: CodeExecutor, record: RunRecord, owner: str) -> ExecutionResult:
    """Await the admitted job without transferring its cancellation ownership."""
    if record.result is not None:
        return record.result
    job = executor._jobs.get(record.id)
    if job is not None:
        return (await asyncio.shield(job)).model_copy(deep=True)
    latest = await executor._runs.get(record.id, owner)
    if latest is not None and latest.result is not None:
        return latest.result
    raise ExecutionError("Run has no active execution in this process")


async def projection_inputs(
    executor: CodeExecutor, artifact_id: str, inputs: dict[str, Any], scope: ExecutionScope
) -> dict[str, Any]:
    """Load only after local/shared admission, within the execution deadline."""
    value = await executor.artifacts.load_json(artifact_id, scope.owner, executor._config.max_response_size_bytes)
    combined = {"artifact": value, "params": inputs}
    json_bytes(combined, executor._config.max_response_size_bytes)
    return combined


def offline_sandbox(executor: CodeExecutor) -> RestrictedSandbox:
    """Construct a fresh restricted backend with no broker reference or callback grant."""
    return RestrictedSandbox(executor._config, executor._registry, None)
