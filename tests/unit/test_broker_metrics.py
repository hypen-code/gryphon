"""Task-local broker accounting counts accepted payloads, never response content."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gryphon.errors import ExecutionError
from gryphon.models import EndpointManifest, ExecutionScope, ServerManifest
from gryphon.runtime.execution_metrics import RunMeasurements, broker_measurements, current_measurements
from gryphon.runtime.execution_validation import json_bytes
from gryphon.security.broker import ToolBroker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from gryphon.config import GryphonConfig


@pytest.fixture
async def measured_broker(gryphon_config: GryphonConfig) -> AsyncIterator[tuple[ToolBroker, AsyncMock]]:
    """Mock only network dispatch; keep request/response validation authoritative."""
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
    broker = ToolBroker(gryphon_config, registry, allow_environment=False)
    network = AsyncMock(return_value=httpx.Response(200, json=[]))
    with patch.object(broker._network, "request", network):
        try:
            yield broker, network
        finally:
            await broker.close()


@contextmanager
def _measuring(run_id: str = "run") -> Iterator[tuple[ExecutionScope, RunMeasurements]]:
    """Bind counters explicitly and always restore the previous async context."""
    scope = ExecutionScope(run_id=run_id, deadline=time.monotonic() + 30)
    state = RunMeasurements(run_id, "execute", "restricted", time.monotonic(), 0, 0, 0)
    token = current_measurements.set(state)
    try:
        yield scope, state
    finally:
        current_measurements.reset(token)


@pytest.mark.parametrize("payload,items", [(None, 0), ([], 0), ([1, 2], 2), ({"nested": [1, 2]}, 0), ("é", 0)])
async def test_broker_metrics_canonical_json_and_top_level_items(
    measured_broker: tuple[ToolBroker, AsyncMock],
    payload: object,
    items: int,
) -> None:
    broker, network = measured_broker
    network.return_value = httpx.Response(200, content=json_bytes(payload, 100_000))
    with _measuring() as (scope, state):
        assert await broker.invoke("svc", "lookup", {}, scope) == payload
    assert state.api_responses == 1 and state.upstream_items == items
    assert state.upstream_bytes == len(json_bytes(payload, 100_000)) and state.broker_ms > 0
    assert current_measurements.get() is None


async def test_broker_metrics_204_is_accepted_null(measured_broker: tuple[ToolBroker, AsyncMock]) -> None:
    broker, network = measured_broker
    network.return_value = httpx.Response(204)
    with _measuring() as (scope, state):
        assert await broker.invoke("svc", "lookup", {}, scope) is None
    assert state.api_responses == 1 and state.upstream_bytes == 4 and state.upstream_items == 0


async def test_broker_metrics_rejected_payload_never_counts_response(
    measured_broker: tuple[ToolBroker, AsyncMock],
) -> None:
    broker, network = measured_broker
    network.return_value = httpx.Response(200, content=b"private invalid data")
    with _measuring() as (scope, state), pytest.raises(ExecutionError):
        await broker.invoke("svc", "lookup", {}, scope)
    assert scope.calls == 1 and state.api_responses == state.upstream_bytes == 0
    assert state.broker_ms > 0


async def test_broker_metrics_schema_rejection_is_not_accepted(measured_broker: tuple[ToolBroker, AsyncMock]) -> None:
    broker, _ = measured_broker
    endpoint = broker._registry.get_endpoint("svc", "lookup")
    endpoint.output_schema = {"type": "object"}
    with _measuring() as (scope, state), pytest.raises(ExecutionError):
        await broker.invoke("svc", "lookup", {}, scope)
    assert scope.calls == 1 and state.api_responses == 0 and state.upstream_bytes == 0


async def test_broker_metrics_concurrent_calls_share_one_run(measured_broker: tuple[ToolBroker, AsyncMock]) -> None:
    broker, network = measured_broker
    network.return_value = httpx.Response(200, json=[1, 2])
    with _measuring() as (scope, state):
        await asyncio.gather(broker.invoke("svc", "lookup", {}, scope), broker.invoke("svc", "lookup", {}, scope))
    assert scope.calls == state.api_responses == 2
    assert state.upstream_items == 4 and state.upstream_bytes == 10


async def test_broker_metrics_concurrent_runs_are_isolated(measured_broker: tuple[ToolBroker, AsyncMock]) -> None:
    broker, _ = measured_broker

    async def run(run_id: str, calls: int) -> RunMeasurements:
        """Child task binds its own counters instead of sharing process totals."""
        with _measuring(run_id) as (scope, state):
            for _ in range(calls):
                await broker.invoke("svc", "lookup", {}, scope)
            return state

    first, second = await asyncio.gather(run("first", 1), run("second", 2))
    assert first.api_responses == 1 and second.api_responses == 2 and first is not second
    assert current_measurements.get() is None


async def test_broker_metrics_measurement_failure_does_not_reject_payload(
    measured_broker: tuple[ToolBroker, AsyncMock],
) -> None:
    broker, _ = measured_broker
    with (
        _measuring() as (scope, state),
        patch("gryphon.runtime.execution_metrics.json_bytes", side_effect=RuntimeError("not retained")),
    ):
        assert await broker.invoke("svc", "lookup", {}, scope) == []
    assert state.api_responses == 1 and not state.upstream_measured and state.upstream_bytes == 0


def test_broker_metrics_foreign_scope_cannot_attach_to_context() -> None:
    with _measuring():
        assert broker_measurements(ExecutionScope(run_id="foreign", deadline=time.monotonic() + 30)) is None
