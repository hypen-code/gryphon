"""Bounded backend work and terminal persistence for the executor-owned job."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from gryphon.errors import ConflictError
from gryphon.models import ExecutionResult
from gryphon.runtime.execution_cleanup import SlotLease, finish_cleanup
from gryphon.runtime.execution_metrics import current_measurements, observe
from gryphon.runtime.execution_results import failure

if TYPE_CHECKING:
    from gryphon.models import ExecutionScope, RunRecord
    from gryphon.runtime.execution_metrics import RunMeasurements
    from gryphon.runtime.executor import CodeExecutor

_RESTRICTED_SECONDS = 30


async def work(
    executor: CodeExecutor,
    record: RunRecord,
    scope: ExecutionScope,
    code: str,
    source: str,
    description: str,
    inputs: dict[str, Any],
    schema: dict[str, Any],
    identity: str,
) -> ExecutionResult:
    """Bound queue wait, execute once, and durably persist a terminal receipt."""
    started, slots = time.monotonic(), SlotLease(executor._semaphore, executor._execution_slots)
    state = executor._measurements.get(record.id)
    token = current_measurements.set(state) if state is not None else None
    cancelled = False
    try:
        await slots.acquire_local(scope.deadline)
        await executor._runs.start(record.id, record.owner)
        await slots.acquire_shared(scope.deadline)
        if identity != executor._fingerprint():
            raise ConflictError("Catalog or execution profile changed after admission")
        duration = executor._config.execution_timeout_seconds
        duration = min(duration, _RESTRICTED_SECONDS) if executor._config.sandbox_mode == "restricted" else duration
        scope.deadline = time.monotonic() + duration
        executor._guard(code)
        async with asyncio.timeout_at(scope.deadline):
            result = await _backend(executor, source, inputs, scope, state)
            result.run_id, result.tool_calls = record.id, scope.calls
            result.execution_time_ms = int((time.monotonic() - started) * 1000)
            result = await executor._bound_result(result, record.owner)
            if state is not None:
                state.artifact_created = result.artifact_id is not None
            slots.release_shared()
            await _cache_success(executor, result, record, code, description, schema, identity)
    except asyncio.CancelledError:
        result = ExecutionResult(success=False, error="Execution cancelled", error_type="cancelled")
        cancelled = True
    except Exception as exc:
        result = failure(exc, record.id)
    finally:
        scope.cancelled = True
        slots.release()
        if token is not None:
            current_measurements.reset(token)
    result.run_id, result.tool_calls = record.id, scope.calls
    await finish_cleanup(persist_terminal(executor, scope, result, "cancelled" if cancelled else None))
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _cache_success(
    executor: CodeExecutor,
    result: ExecutionResult,
    record: RunRecord,
    code: str,
    description: str,
    schema: dict[str, Any],
    identity: str,
) -> None:
    """Preserve success-only recipe persistence after bounded output and shared-slot release."""
    # Cache on success
    if result.success and executor._config.cache_enabled:
        result.cache_id = await executor._cache.store(
            code,
            description,
            sorted(server.name for server in executor._registry.list_servers()),
            identity,
            owner=record.owner,
            input_schema=schema,
        )


async def _backend(
    executor: CodeExecutor,
    source: str,
    inputs: dict[str, Any],
    scope: ExecutionScope,
    state: RunMeasurements | None,
) -> ExecutionResult:
    """Measure backend wall time, including awaited broker work, never CPU time."""
    if state is not None:
        state.backend_start = time.monotonic()
    try:
        result = await executor._sandbox.run(source, inputs, scope)
    finally:
        if state is not None:
            state.backend_end = time.monotonic()
    if state is not None:
        state.measure_result(result, executor._config.max_response_size_bytes)
    return result


async def persist_terminal(
    executor: CodeExecutor,
    scope: ExecutionScope,
    result: ExecutionResult,
    status: str | None = None,
) -> None:
    """Commit the receipt before observing, and release per-run measurement ownership."""
    try:
        await executor._runs.finish(scope.run_id, scope.owner, result, status=status)
        state = executor._measurements.get(scope.run_id)
        if state is not None and executor._analytics is not None:
            await observe(executor._analytics, state, result, scope)
    finally:
        executor._measurements.pop(scope.run_id, None)
