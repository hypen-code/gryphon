"""Bounded hosted wire observations and cancellation-resistant optional persistence."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from starlette.requests import Request

from gryphon.models import Channel
from gryphon.saas_gateway import MCPGateway, _successful, _tool
from gryphon.saas_traffic import _RESPONSE_LIMIT, TrafficObservation, record_traffic

if TYPE_CHECKING:
    from starlette.types import Message, Receive, Scope, Send

    from gryphon.models.traffic import RequestMetrics
    from gryphon.saas_runtime import ChannelRuntimeManager
    from gryphon.saas_store import SaaSStore


def _json(value: object) -> bytes:
    """Independently encode the documented strict canonical UTF-8 representation."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _observation(body: bytes, *, status: int = 200, finished: bool = True) -> TrafficObservation:
    """Deliver ASGI messages without retaining any unbounded auxiliary response buffer."""
    observation = TrafficObservation(73)
    observation.observe({"type": "http.response.start", "status": status, "headers": []})
    for offset in range(0, len(body), 101):
        end = offset + 101
        observation.observe(
            {"type": "http.response.body", "body": body[offset:end], "more_body": end < len(body) or not finished}
        )
    return observation


def test_traffic_chunked_utf8_counts_one_canonical_payload_without_text_duplicate() -> None:
    """Split multibyte characters across chunks and distinguish wire from structured bytes."""
    payload = {"success": True, "data": {"z": "界é" * 16000, "a": [None, False, 7]}}
    body = _json(
        {
            "jsonrpc": "2.0",
            "id": "untrusted",
            "result": {
                "structuredContent": payload,
                "content": [{"type": "text", "text": _json(payload).decode()}],
            },
        }
    )
    observation = _observation(body)
    metric = observation.snapshot("execute_code", _successful(observation.status, observation.buffer))
    assert metric.request_bytes == 73 and metric.response_bytes == len(body)
    assert metric.payload_bytes == len(_json(payload)) < len(body)
    assert metric.success and metric.error_type is None and metric.observation_complete
    assert metric.duration_ms >= 0 and UUID(metric.request_id).version == 4
    assert observation.buffer == bytearray()
    assert "界" not in metric.model_dump_json() and "untrusted" not in metric.model_dump_json()


def test_traffic_oversize_counts_every_wire_byte_and_erases_parse_buffer() -> None:
    """Exceed the fixed observation budget without losing accounting of later chunks."""
    body = _json({"result": {"structuredContent": {"data": "界" * _RESPONSE_LIMIT}}})
    observation = _observation(body)
    assert len(observation.buffer) <= _RESPONSE_LIMIT and observation.truncated
    metric = observation.snapshot("read_artifact", False)
    assert metric.response_bytes == len(body) and metric.payload_bytes == 0
    assert not metric.observation_complete and not metric.success
    assert observation.buffer == bytearray()


@pytest.mark.parametrize(
    ("body", "status", "complete", "category"),
    [
        (b'{"error":{"code":-32602}}', 200, True, "protocol"),
        (b'{"result":{"isError":true}}', 200, True, "protocol"),
        (b"{}", 503, True, "protocol"),
        (b"{}", 200, True, "protocol"),
        (b'{"jsonrpc":"2.0","id":7}', 200, True, "protocol"),
        (b"{", 200, False, "protocol"),
        (b"[]", 200, True, "protocol"),
    ],
)
def test_traffic_protocol_and_malformed_results_are_static_failures(
    body: bytes, status: int, complete: bool, category: str
) -> None:
    """Protocol failure is independent of HTTP success and malformed measurement is explicit."""
    observation = _observation(body, status=status)
    metric = observation.snapshot("execute_code", _successful(status, observation.buffer))
    assert not metric.success and metric.error_type == category
    assert metric.observation_complete is complete and metric.payload_bytes == 0
    assert metric.response_bytes == len(body) and not observation.buffer


def test_traffic_unfinished_transport_is_incomplete_even_with_valid_json() -> None:
    """A response body without a final ASGI frame cannot become a completed request."""
    observation = _observation(b'{"result":{"structuredContent":{"data":42}}}', finished=False)
    metric = observation.snapshot("get_run", False)
    assert not metric.success and not metric.observation_complete and metric.error_type == "transport"
    assert metric.payload_bytes == len(b'{"data":42}') and not observation.buffer


@pytest.mark.parametrize("category", ["timeout", "cache", "conflict", "cache_miss", "stale", "private-error-text"])
def test_traffic_error_categories_are_allowlisted(category: str) -> None:
    """Unknown execution messages cannot create dimensions or persist their raw string."""
    observation = _observation(_json({"result": {"structuredContent": {"success": False, "error_type": category}}}))
    metric = observation.snapshot("run_cached_code", False)
    assert metric.error_type == ("unknown" if category == "private-error-text" else category)
    assert "private-error-text" not in metric.model_dump_json()
    assert not metric.success and metric.observation_complete and not observation.buffer


def test_traffic_successful_poll_does_not_inherit_nested_execution_failure() -> None:
    """Fetching a failed run succeeds at the RPC layer and never counts another run failure."""
    payload = {"status": "failed", "result": {"success": False, "error_type": "execution"}}
    observation = _observation(_json({"result": {"structuredContent": payload}}))
    assert _successful(observation.status, observation.buffer)
    metric = observation.snapshot("get_run", True)
    assert metric.success and metric.error_type is None and metric.payload_bytes == len(_json(payload))


@pytest.mark.parametrize(
    ("method", "name", "expected"),
    [
        ("tools/call", "get_run", "get_run"),
        ("tools/call", "secret", None),
        ("tools/list", "execute_code", None),
        ("initialize", "execute_code", None),
        ("notifications/cancelled", "cancel_run", None),
    ],
)
def test_traffic_recognizes_only_known_tool_call_methods(method: str, name: str, expected: str | None) -> None:
    """Neither discovery nor arbitrary client labels become analytics dimensions."""
    assert _tool(_json({"method": method, "params": {"name": name}})) == expected


async def test_traffic_store_failure_is_nonfatal_and_clears_sensitive_buffer() -> None:
    """Optional persistence failure does not replace an already delivered response."""
    store = AsyncMock()
    store.record_request.side_effect = RuntimeError("private storage failure")
    observation = _observation(b'{"result":{"structuredContent":{"data":"private"}}}')
    await record_traffic(store, "tenant", "channel", observation, "execute_code", True)
    store.record_request.assert_awaited_once()
    metric = store.record_request.call_args.args[2]
    assert metric.success and not observation.buffer and "private" not in metric.model_dump_json()


async def test_traffic_cancellation_waits_for_owned_write_then_propagates() -> None:
    """Caller cancellation does not orphan a write already owned by telemetry cleanup."""
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def persist(tenant: str, channel: str, metric: RequestMetrics) -> bool:
        """Hold a simulated write until the test explicitly permits durable completion."""
        assert (tenant, channel, metric.tool) == ("tenant", "channel", "get_run")
        entered.set()
        await release.wait()
        committed.set()
        return True

    store = AsyncMock()
    store.record_request.side_effect = persist
    async with asyncio.timeout(3):
        task = asyncio.create_task(record_traffic(store, "tenant", "channel", _observation(b"{}"), "get_run", True))
        try:
            await entered.wait()
            task.cancel()
            assert not committed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert committed.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_traffic_write_deadline_cancels_and_joins_stalled_persistence() -> None:
    """The telemetry deadline owns cancellation and cleanup without arbitrary sleeps."""
    entered, finalized = asyncio.Event(), asyncio.Event()

    async def persist(*args: object) -> bool:
        """Block until the real timeout cancels the pending storage operation."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return True

    store = AsyncMock()
    store.record_request.side_effect = persist
    with patch("gryphon.saas_traffic._WRITE_TIMEOUT_SECONDS", 0.02):
        async with asyncio.timeout(2):
            await record_traffic(store, "tenant", "channel", _observation(b"{}"), "get_run", True)
    assert entered.is_set() and finalized.is_set()


async def test_gateway_delivers_sdk_response_after_optional_failing_traffic_write() -> None:
    """A failed analytics insert cannot alter the produced response awaiting final handoff."""
    sent: list[Message] = []
    store, analytics = AsyncMock(), AsyncMock()
    body = _json({"method": "tools/call", "params": {"name": "get_run"}})
    response = _json({"result": {"structuredContent": {"status": "failed", "result": {"success": False}}}})

    async def persist(*args: object) -> None:
        """Require persistence failure before the final response frame reaches the transport."""
        assert sent == [{"type": "http.response.start", "status": 200, "headers": []}]
        raise RuntimeError("storage unavailable")

    async def sdk(scope: Scope, receive: Receive, send: Send) -> None:
        """Produce a native JSON-RPC response through the real gateway observation wrapper."""
        assert (await receive())["body"] == body and scope["path"] == "/"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": response})

    async def send(message: Message) -> None:
        """Save only test response frames accepted by the transport."""
        sent.append(message)

    analytics.record_request.side_effect = persist
    gateway = MCPGateway(cast("SaaSStore", store), cast("ChannelRuntimeManager", AsyncMock()), analytics)
    channel = Channel(id="channel", tenant_id="tenant", name="test", created_at=0)
    request = Request(
        {"type": "http", "path": "/mcp/channel", "headers": []},
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        ),
    )
    await gateway._dispatch(request, sdk, channel, send)
    assert len(sent) == 2 and sent[-1]["body"] == response
    analytics.record_request.assert_awaited_once()
    metric = analytics.record_request.call_args.args[2]
    assert metric.success and metric.error_type is None and metric.response_bytes == len(response)
    assert metric.request_bytes == len(body)
    assert store.record_usage.call_args.args[:4] == ("tenant", "channel", "get_run", "success")
