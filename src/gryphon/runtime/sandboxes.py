"""Gryphon restricted VM backend: no host Python, imports, mounts, or OS access."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import TYPE_CHECKING, Any

from pydantic_monty import Monty, MontyError, MontySyntaxError

from gryphon.errors import (
    CapacityError,
    ExecutionError,
    ExecutionTimeoutError,
    InputValidationError,
    SecurityViolationError,
)
from gryphon.models import ExecutionResult
from gryphon.runtime.docker_sandbox import DockerSandbox
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from gryphon.config import GryphonConfig
    from gryphon.models import ExecutionScope
    from gryphon.runtime.registry import Registry
    from gryphon.security.broker import ToolBroker

__all__ = ["DockerSandbox", "RestrictedSandbox"]
logger = get_logger(__name__)
_CAPABILITY = re.compile(r"([a-z][a-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)\Z")
_HARD_DEADLINE_SECONDS = 30


class RestrictedSandbox:
    """Execute isolated Monty programs with one revocable broker capability."""

    def __init__(self, config: GryphonConfig, registry: Registry, broker: ToolBroker) -> None:
        """Retain only server-owned policy and broker state; never reuse a VM."""
        self._config = config
        self._registry = registry
        self._broker = broker

    async def startup(self) -> None:
        """Initialize without connecting to Docker or granting OS access."""

    async def close(self) -> None:
        """Release backend resources after the executor has drained its jobs."""

    async def run(self, code: str, inputs: dict[str, Any], scope: ExecutionScope) -> ExecutionResult:
        """Run a fresh bounded VM and await its termination even on cancellation.

        Args:
            code: Guarded source with an explicit final expression.
            inputs: JSON-only detached input dictionary.
            scope: Mutable, server-owned authority with a monotonic deadline.

        Returns:
            JSON-native output with diagnostic print content deliberately omitted.
        """
        if scope.cancelled or not math.isfinite(scope.deadline) or scope.deadline <= time.monotonic():
            raise ExecutionTimeoutError("Execution scope expired")
        scope.deadline = min(scope.deadline, time.monotonic() + _HARD_DEADLINE_SECONDS)
        callbacks: set[asyncio.Task[Any]] = set()
        state: dict[str, Any] = {"calls": 0, "printed": 0, "overflow": False, "failure": None}
        invoke = self._capability(scope, callbacks, state)
        try:
            runner = await Monty.acreate(code, script_name="gryphon.py", inputs=["inputs"])
        except MontyError as exc:
            raise _vm_error(exc) from None
        vm = asyncio.ensure_future(
            runner.run_async(
                inputs={"inputs": inputs},
                external_functions={"call_tool": invoke},
                print_callback=self._printer(scope, state),
                limits={
                    "max_duration_secs": max(0.001, scope.deadline - time.monotonic()),
                    "max_memory": self._config.sandbox_memory_bytes,
                    "max_recursion_depth": 128,
                },
            )
        )
        try:
            data = await asyncio.shield(vm)
            self._check_result(scope, state)
            json_bytes(data, self._config.max_response_size_bytes)
            prints = f"Sandbox printed {state['printed']} bytes (content omitted)." if state["printed"] else None
            return ExecutionResult(success=True, data=data, prints=prints)
        except MontyError as exc:
            if state["failure"] is not None:
                raise state["failure"] from None
            if state["overflow"]:
                raise CapacityError("Sandbox print output exceeds limit") from None
            raise _vm_error(exc) from None
        finally:
            scope.cancelled = True
            await finish_cleanup(self._settle(vm, callbacks, scope, state))

    async def _settle(
        self,
        vm: asyncio.Future[Any],
        callbacks: set[asyncio.Task[Any]],
        scope: ExecutionScope,
        state: dict[str, Any],
    ) -> None:
        """Drain callbacks and the native VM before publishing its final call counter."""
        for callback in tuple(callbacks):
            callback.cancel()
        await asyncio.gather(*tuple(callbacks), return_exceptions=True)
        await asyncio.gather(vm, return_exceptions=True)
        scope.calls = max(scope.calls, min(state["calls"], scope.max_calls))

    def _check_result(self, scope: ExecutionScope, state: dict[str, Any]) -> None:
        """Prevent caught broker errors or producer overflow from becoming success."""
        if state["failure"] is not None:
            raise state["failure"]
        if state["overflow"]:
            raise CapacityError("Sandbox print output exceeds limit")
        if scope.cancelled or time.monotonic() >= scope.deadline:
            raise ExecutionTimeoutError("Execution deadline exceeded")

    def _printer(self, scope: ExecutionScope, state: dict[str, Any]) -> Callable[[str, str], None]:
        """Count bytes without storing raw printed values or exception messages."""

        def capture(stream: str, text: str) -> None:
            """Bound VM stdout at the producer, not after building an output string."""
            state["printed"] += len(text.encode("utf-8"))
            if state["printed"] > self._config.max_output_size_bytes:
                state["overflow"] = True
                raise CapacityError("Sandbox print output exceeds limit")
            if scope.cancelled or time.monotonic() >= scope.deadline:
                raise ExecutionTimeoutError("Execution deadline exceeded")

        return capture

    def _capability(
        self, scope: ExecutionScope, callbacks: set[asyncio.Task[Any]], state: dict[str, Any]
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Construct the single broker route with independent call-budget enforcement."""

        async def invoke(capability: str, arguments: dict[str, Any] | None = None) -> Any:
            """Validate every route against the catalog and revoke before cancellation."""
            task = asyncio.current_task()
            if task is not None:
                callbacks.add(task)
            try:
                if scope.cancelled or time.monotonic() >= scope.deadline:
                    raise ExecutionTimeoutError("Execution scope expired")
                state["calls"] += 1
                if state["calls"] > scope.max_calls:
                    raise CapacityError("Tool call budget exhausted")
                match = _CAPABILITY.fullmatch(capability) if isinstance(capability, str) else None
                if match is None:
                    raise InputValidationError("Capability must be server.function")
                server, function = match.groups()
                self._registry.get_function(server, function)
                if arguments is not None and type(arguments) is not dict:
                    raise InputValidationError("Tool arguments must be a JSON object")
                json_bytes(arguments or {}, self._config.max_response_size_bytes)
                result = await self._broker.invoke(server, function, arguments or {}, scope)
                if scope.cancelled or time.monotonic() >= scope.deadline:
                    raise ExecutionTimeoutError("Execution scope expired")
                json_bytes(result, self._config.max_response_size_bytes)
                return result
            except Exception as exc:
                scope.cancelled = True
                if state["failure"] is None:
                    state["failure"] = exc
                logger.warning("sandbox_capability_failed", error_type=type(exc).__name__)
                raise RuntimeError("Broker capability failed") from None
            finally:
                if task is not None:
                    callbacks.discard(task)

        return invoke


def _vm_error(exc: MontyError) -> Exception:
    """Map VM errors by type only, never expose code, values, or tracebacks."""
    inner = exc.exception()
    if isinstance(inner, TimeoutError):
        return ExecutionTimeoutError("Restricted execution timed out")
    if isinstance(inner, (MemoryError, RecursionError)):
        return CapacityError("Restricted VM resource limit exceeded")
    if isinstance(exc, MontySyntaxError):
        return InputValidationError("Code uses invalid or unsupported restricted Python syntax")
    if isinstance(inner, (ImportError, PermissionError)):
        return SecurityViolationError("Restricted VM capability is not available")
    return ExecutionError("Restricted Python execution failed")
