"""Content-free analytics with real Monty, broker validation and durable receipts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from gryphon.models import CacheEntry, EndpointManifest, RunMetrics, ServerManifest
from gryphon.runtime.execution_metrics import current_measurements
from gryphon.runtime.execution_validation import json_bytes
from gryphon.runtime.executor import CodeExecutor
from gryphon.security.broker import ToolBroker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


@dataclass
class MetricsHarness:
    """Isolated executor and controllable network, without live upstream traffic."""

    executor: CodeExecutor
    records: list[RunMetrics]
    network: AsyncMock
    registry: MagicMock


@pytest.fixture
async def metrics_executor(gryphon_config: GryphonConfig) -> AsyncIterator[MetricsHarness]:
    """Retain real broker/result validation and SQLite receipt persistence."""
    config = gryphon_config
    config.cache_enabled = False
    endpoint = EndpointManifest(
        function_name="lookup",
        summary="Lookup",
        method="GET",
        path="/items",
        parameters_summary="",
        response_summary="",
        input_schema={"type": "object", "additionalProperties": False},
    )
    registry = MagicMock()
    registry.fingerprint.return_value = "catalog"
    registry.list_servers.return_value = []
    registry.get_endpoint.return_value = endpoint
    registry.get_manifest.return_value = ServerManifest(
        server_name="svc",
        description="API",
        swagger_hash="catalog",
        compiled_at="now",
        base_url="https://api.example.com",
        is_read_only=True,
        endpoints=[endpoint],
    )
    broker = ToolBroker(config, registry, allow_environment=False)
    records: list[RunMetrics] = []

    async def observer(metrics: RunMetrics) -> None:
        """Assert observers cannot see a preterminal receipt."""
        receipt = await executor.get_run(metrics.run_id)
        assert receipt is not None and receipt.status == metrics.status
        records.append(metrics)

    executor = CodeExecutor(config, AsyncMock(), registry, broker, analytics=observer)
    network = AsyncMock(return_value=httpx.Response(200, json=[{"value": 1}, {"value": 2}]))
    with patch.object(broker._network, "request", network):
        await executor.startup()
        try:
            yield MetricsHarness(executor, records, network, registry)
        finally:
            await executor.shutdown()


async def test_metrics_real_monty_reduction_measures_before_user_code(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    code = 'data = await call_tool("svc.lookup", {})\nresult = data[0]["value"]'
    result = await harness.executor.execute(code, "private-description", {"private-input": "omitted"})
    (metrics,) = harness.records
    assert result.success and result.data == 1
    assert metrics.origin == "execute" and metrics.status == "succeeded"
    assert metrics.upstream_bytes == len(b'[{"value":1},{"value":2}]')
    assert metrics.upstream_items == 2 and metrics.result_bytes == 1 and metrics.result_items is None
    assert metrics.api_calls == metrics.api_responses == 1 and metrics.comparison_eligible
    assert metrics.source_bytes == len(code.encode()) and metrics.source_lines == 2
    assert metrics.input_bytes == len(b'{"private-input":"omitted"}')
    assert metrics.duration_ms >= metrics.execution_ms >= metrics.broker_ms > 0
    assert metrics.duration_ms >= metrics.queue_ms >= 0 and metrics.backend_started
    assert current_measurements.get() is None
    assert not {"code", "inputs", "data", "prints", "headers", "url", "source_hash"} & RunMetrics.model_fields.keys()
    assert "private" not in metrics.model_dump_json() and "omitted" not in metrics.model_dump_json()


async def test_metrics_fanout_aggregates_both_accepted_responses(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.network.side_effect = [httpx.Response(200, json=[1, 2]), httpx.Response(200, json=[3])]
    result = await harness.executor.execute(
        'first = await call_tool("svc.lookup", {})\n'
        'second = await call_tool("svc.lookup", {})\nresult = first + second',
        "fanout",
    )
    (metrics,) = harness.records
    assert result.data == [1, 2, 3]
    assert metrics.api_calls == metrics.api_responses == 2 and metrics.upstream_items == 3
    assert metrics.upstream_bytes == len(b"[1,2][3]")
    assert metrics.result_bytes == len(b"[1,2,3]") and metrics.result_items == 3


@pytest.mark.parametrize("expression,size,items", [("None", 4, None), ("[]", 2, 0), ("{}", 2, None), ("[1,2]", 5, 2)])
async def test_metrics_compute_only_has_no_upstream_comparison(
    metrics_executor: MetricsHarness,
    expression: str,
    size: int,
    items: int | None,
) -> None:
    harness = metrics_executor
    result = await harness.executor.execute(f"result = {expression}", "compute")
    (metrics,) = harness.records
    assert result.success and metrics.result_bytes == size and metrics.result_items == items
    assert metrics.api_calls == metrics.api_responses == metrics.upstream_bytes == metrics.upstream_items == 0
    assert not metrics.comparison_eligible and metrics.broker_ms == 0


async def test_metrics_artifact_measures_full_raw_final_data(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.executor._config.max_output_size_bytes = 1024
    payload = ["item" * 50 for _ in range(20)]
    harness.network.return_value = httpx.Response(200, json=payload)
    result = await harness.executor.execute('result = await call_tool("svc.lookup", {})', "artifact")
    (metrics,) = harness.records
    assert result.success and result.artifact_id is not None and metrics.artifact_created
    assert metrics.result_bytes == metrics.upstream_bytes == len(json_bytes(payload, 100_000))
    assert metrics.result_items == metrics.upstream_items == 20 and metrics.comparison_eligible
    assert metrics.result_bytes > len(json_bytes(result.data, 100_000))


async def test_metrics_idempotent_duplicate_never_counts_another_run(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    first = await harness.executor.execute("result = 1", "once", idempotency_key="same")
    duplicate = await harness.executor.execute("result = 1", "once", idempotency_key="same")
    assert first.run_id == duplicate.run_id and len(harness.records) == 1


async def test_metrics_background_submit_records_terminal_without_polling(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    receipt = await harness.executor.submit("result = 1", "background")
    job = harness.executor._jobs[receipt.id]
    await job
    (metrics,) = harness.records
    assert metrics.run_id == receipt.id and metrics.origin == "submit" and metrics.status == "succeeded"


async def test_metrics_replay_retains_origin(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    entry = CacheEntry(
        id="recipe",
        code="result = inputs",
        description="replay",
        swagger_hash=harness.executor._fingerprint(),
        created_at=time.time(),
        last_used_at=time.time(),
    )
    with patch.object(harness.executor._cache, "get", AsyncMock(return_value=entry)):
        result = await harness.executor.replay("recipe", {"n": 1})
    (metrics,) = harness.records
    assert result.success and metrics.origin == "replay"


async def test_metrics_observer_failure_does_not_change_success(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.executor._analytics = AsyncMock(side_effect=RuntimeError("private observer failure"))
    result = await harness.executor.execute("result = 1", "failure isolation")
    assert result.success and result.error is None and not harness.executor._measurements


async def test_metrics_observer_timeout_is_bounded(metrics_executor: MetricsHarness) -> None:
    async def observer(metrics: RunMetrics) -> None:
        """Simulate an unavailable metrics store."""
        await asyncio.Event().wait()

    harness = metrics_executor
    harness.executor._analytics = observer
    with patch("gryphon.runtime.execution_metrics._OBSERVER_TIMEOUT_SECONDS", 0.01):
        result = await asyncio.wait_for(harness.executor.execute("result = 1", "bounded observer"), 2)
    assert result.success and not harness.executor._measurements


async def test_metrics_queue_timeout_marks_backend_unstarted(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.executor._execution_slots = asyncio.Semaphore(0)
    receipt = await harness.executor.submit("result = 1", "queued")
    harness.executor._scopes[receipt.id].deadline = time.monotonic() - 1
    result = await harness.executor._jobs[receipt.id]
    (metrics,) = harness.records
    assert result.error_type == "capacity" and metrics.error_type == "capacity"
    assert not metrics.backend_started and metrics.execution_ms == 0 and metrics.queue_ms > 0


async def test_metrics_backend_timeout_is_not_queue_time(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.network.side_effect = TimeoutError
    result = await harness.executor.execute('result = await call_tool("svc.lookup", {})', "timeout")
    (metrics,) = harness.records
    assert result.error_type == "timeout" and metrics.error_type == "timeout"
    assert metrics.backend_started and metrics.execution_ms > 0
    assert metrics.api_calls == 1 and metrics.api_responses == 0 and not metrics.comparison_eligible


@pytest.mark.parametrize("before_start", [True, False])
async def test_metrics_cancelled_job_is_observed_once(metrics_executor: MetricsHarness, before_start: bool) -> None:
    harness = metrics_executor
    harness.executor._execution_slots = asyncio.Semaphore(0)
    receipt = await harness.executor.submit("result = 1", "cancelled")
    job = harness.executor._jobs[receipt.id]
    if before_start:
        job.cancel()
    await harness.executor.cancel(receipt.id)
    await harness.executor._finish_cancelled(receipt.id, "local")
    (metrics,) = harness.records
    assert metrics.status == "cancelled" and metrics.error_type == "cancelled"
    assert not metrics.backend_started and metrics.execution_ms == 0 and job.cancelled()
    assert not harness.executor._measurements


async def test_metrics_backend_cancellation_awaits_observer(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    entered, observed = asyncio.Event(), asyncio.Event()

    async def waiting(*args: object, **kwargs: object) -> httpx.Response:
        """Block an accepted request until its execution is cancelled."""
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json=[])

    async def observer(metrics: RunMetrics) -> None:
        """Persist after cancellation without losing the terminal callback."""
        harness.records.append(metrics)
        observed.set()

    harness.executor._analytics = observer
    harness.network.side_effect = waiting
    task = asyncio.create_task(harness.executor.execute('result = await call_tool("svc.lookup", {})', "cancel"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (metrics,) = harness.records
    assert observed.is_set() and metrics.status == "cancelled" and metrics.backend_started
    assert metrics.api_responses == 0 and metrics.broker_ms > 0


async def test_metrics_caller_cancellation_during_observer_waits_for_persistence(
    metrics_executor: MetricsHarness,
) -> None:
    harness = metrics_executor
    entered, release = asyncio.Event(), asyncio.Event()

    async def observer(metrics: RunMetrics) -> None:
        """Hold terminal analytics persistence until the test releases its transaction."""
        entered.set()
        await release.wait()
        harness.records.append(metrics)

    harness.executor._analytics = observer
    task = asyncio.create_task(harness.executor.execute("result = 1", "terminal observer"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(harness.records) == 1 and harness.records[0].status == "succeeded"


async def test_metrics_catalog_drift_is_unstarted_failure(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    receipt = await harness.executor.submit("result = 1", "drift")
    harness.registry.fingerprint.return_value = "changed"
    result = await harness.executor._jobs[receipt.id]
    (metrics,) = harness.records
    assert result.error_type == "conflict" and not metrics.backend_started


async def test_metrics_disabled_skips_measurement_serialization(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.executor._analytics = None
    with patch("gryphon.runtime.execution_metrics.json_bytes", side_effect=AssertionError("disabled")):
        result = await harness.executor.execute('result = await call_tool("svc.lookup", {})', "no observer")
    assert result.success and not harness.records and not harness.executor._measurements


async def test_metrics_bad_measurement_does_not_change_valid_execution(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    receipt = await harness.executor.submit("result = 1", "measurement isolation")
    with patch("gryphon.runtime.execution_metrics.json_bytes", side_effect=RuntimeError("not retained")):
        result = await harness.executor._jobs[receipt.id]
    (metrics,) = harness.records
    assert result.success and metrics.result_bytes == 0 and not metrics.comparison_eligible


async def test_metrics_admission_measurement_failure_keeps_execution_success(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    with patch("gryphon.runtime.execution_metrics.json_bytes", side_effect=RuntimeError("not retained")):
        result = await harness.executor.execute("result = 1", "admission measurement failure")
    assert result.success and not harness.records and not harness.executor._measurements


async def test_metrics_artifact_flag_survives_later_cache_failure(metrics_executor: MetricsHarness) -> None:
    harness = metrics_executor
    harness.executor._config.max_output_size_bytes = 1024
    harness.executor._config.cache_enabled = True
    with patch.object(harness.executor._cache, "store", AsyncMock(side_effect=RuntimeError("not retained"))):
        result = await harness.executor.execute('result = ["item" * 50 for _ in range(20)]', "artifact cache failure")
    (metrics,) = harness.records
    assert not result.success and metrics.status == "failed" and metrics.artifact_created
    assert metrics.result_bytes > 1024 and not metrics.comparison_eligible


def test_metrics_dto_rejects_content_and_unknown_errors() -> None:
    payload = {"run_id": "run", "origin": "execute", "status": "failed", "sandbox_mode": "restricted"}
    with pytest.raises(ValidationError):
        RunMetrics.model_validate({**payload, "error_type": "private upstream message"})
    with pytest.raises(ValidationError):
        RunMetrics.model_validate({**payload, "source": "result = private"})
