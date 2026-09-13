"""Optional, task-local measurements without credentials or execution authority."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from gryphon.models import RunMetrics
from gryphon.runtime.execution_validation import json_bytes
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from gryphon.config import GryphonConfig
    from gryphon.models import ExecutionResult, ExecutionScope
    from gryphon.models.analytics import RunErrorType, RunOrigin

logger = get_logger(__name__)
_OBSERVER_TIMEOUT_SECONDS = 2
_SAFE_ERRORS = frozenset(
    {
        "capacity",
        "timeout",
        "security",
        "validation",
        "conflict",
        "cache",
        "sandbox_unavailable",
        "not_found",
        "execution",
        "upstream",
        "internal",
        "cancelled",
    }
)


@dataclass
class RunMeasurements:
    """Private per-run scalar counters; no payloads or authority are retained."""

    run_id: str
    origin: RunOrigin
    sandbox_mode: Literal["restricted", "docker"]
    admitted: float
    source_bytes: int
    source_lines: int
    input_bytes: int
    backend_start: float | None = None
    backend_end: float | None = None
    result_bytes: int = 0
    result_items: int | None = None
    result_measured: bool = False
    artifact_created: bool = False
    upstream_bytes: int = 0
    upstream_items: int = 0
    api_responses: int = 0
    broker_ms: float = 0
    upstream_measured: bool = True

    @classmethod
    def create(
        cls, scope: ExecutionScope, origin: RunOrigin, code: str, inputs: object, config: GryphonConfig
    ) -> RunMeasurements | None:
        """Measure validated admission data without making telemetry a failure mode."""
        try:
            return cls(
                scope.run_id,
                origin,
                config.sandbox_mode,
                time.monotonic(),
                len(code.encode("utf-8")),
                len(code.splitlines()),
                len(json_bytes(inputs, config.max_response_size_bytes)),
            )
        except Exception:
            logger.warning("execution_metrics_measurement_failed")
            return None

    def accept_response(self, value: object, limit: int) -> None:
        """Count only a validated broker return; bounded JSON errors stay nonfatal."""
        self.api_responses += 1
        try:
            size = len(json_bytes(value, limit))
            self.upstream_bytes += size
            self.upstream_items += len(value) if isinstance(value, list) else 0
        except Exception:
            self.upstream_measured = False
            logger.warning("execution_metrics_measurement_failed")

    def measure_result(self, result: ExecutionResult, limit: int) -> None:
        """Measure raw final data before artifact summaries replace the backend value."""
        if result.success:
            try:
                self.result_bytes = len(json_bytes(result.data, limit))
                self.result_items = len(result.data) if isinstance(result.data, list) else None
                self.result_measured = True
            except Exception:
                logger.warning("execution_metrics_measurement_failed")

    def snapshot(self, result: ExecutionResult, scope: ExecutionScope) -> RunMetrics:
        """Build a closed scalar DTO, mapping unknown error categories to internal."""
        ended = time.monotonic()
        error = result.error_type
        safe_error = cast("RunErrorType", error if error in _SAFE_ERRORS else "internal") if error else None
        backend_started = self.backend_start is not None
        return RunMetrics(
            run_id=self.run_id,
            origin=self.origin,
            status="succeeded" if result.success else "cancelled" if error == "cancelled" else "failed",
            error_type=safe_error,
            sandbox_mode=self.sandbox_mode,
            duration_ms=max(0, (ended - self.admitted) * 1000),
            queue_ms=max(0, ((self.backend_start or ended) - self.admitted) * 1000),
            execution_ms=max(0, ((self.backend_end or ended) - self.backend_start) * 1000)
            if self.backend_start is not None
            else 0,
            source_bytes=self.source_bytes,
            source_lines=self.source_lines,
            input_bytes=self.input_bytes,
            result_bytes=self.result_bytes,
            result_items=self.result_items,
            upstream_bytes=self.upstream_bytes,
            upstream_items=self.upstream_items,
            api_calls=scope.calls,
            api_responses=self.api_responses,
            broker_ms=self.broker_ms,
            backend_started=backend_started,
            artifact_created=self.artifact_created or result.artifact_id is not None,
            comparison_eligible=result.success
            and backend_started
            and self.api_responses > 0
            and self.result_measured
            and self.upstream_measured,
        )


current_measurements: ContextVar[RunMeasurements | None] = ContextVar("execution_measurements", default=None)


def broker_measurements(scope: ExecutionScope) -> RunMeasurements | None:
    """Return counters only for the matching run, never confer broker authority."""
    state = current_measurements.get()
    return state if state is not None and state.run_id == scope.run_id else None


async def observe(
    callback: Callable[[RunMetrics], Awaitable[None]],
    state: RunMeasurements,
    result: ExecutionResult,
    scope: ExecutionScope,
) -> None:
    """Bound observer persistence; callers shield this with durable terminal cleanup."""
    try:
        metrics = state.snapshot(result, scope)
        async with asyncio.timeout(_OBSERVER_TIMEOUT_SECONDS):
            await callback(metrics)
    except asyncio.CancelledError:
        logger.warning("execution_metrics_observer_cancelled")
        raise
    except Exception:
        logger.warning("execution_metrics_observer_failed")
