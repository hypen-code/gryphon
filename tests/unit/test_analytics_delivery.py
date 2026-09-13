"""Deterministic final-frame ordering for optional hosted request persistence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from starlette.requests import Request

from gryphon.saas_analytics import AnalyticsStore
from gryphon.saas_gateway import MCPGateway
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import Message, Receive, Scope, Send

    from gryphon.models import Channel
    from gryphon.models.traffic import RequestMetrics
    from gryphon.saas_runtime import ChannelRuntimeManager

RESPONSE = b'{"result":{"structuredContent":{"success":true,"data":42}}}'


@dataclass
class Delivery:
    """Own a disposable channel and the frames handed to its response transport."""

    store: SaaSStore
    analytics: AnalyticsStore
    channel: Channel
    received: asyncio.Event = field(default_factory=asyncio.Event)
    frames: list[Message] = field(default_factory=list)

    async def send(self, message: Message) -> None:
        """Mark final handoff and immediately query both committed stores without polling."""
        self.frames.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            self.received.set()
            usage = await self.store.list_usage(self.channel.tenant_id, self.channel.id)
            report = await self.analytics.get_report(self.channel.tenant_id, channel_id=self.channel.id)
            assert len(usage) == 1 and usage[0].tool == "run_cached_code"
            assert usage[0].calls == 1 and usage[0].status == "success"
            assert report["summary"]["requests"] == report["summary"]["request_successes"] == 1
            assert report["summary"]["response_wire_bytes"] == len(RESPONSE)

    async def dispatch(self, send: Send, *, tool: str = "run_cached_code", failure: str = "") -> None:
        """Drive SDK-like chunked output through the actual gateway, including exceptional exits."""
        body = json.dumps({"method": "tools/call", "params": {"name": tool}}).encode()
        request = Request(
            {"type": "http", "path": "/mcp/" + self.channel.id, "headers": []},
            AsyncMock(return_value={"type": "http.request", "body": body}),
        )

        async def sdk(scope: Scope, receive: Receive, observed: Send) -> None:
            """Produce chunks without waiting for gateway dispatch to return."""
            assert (await receive())["body"] == body and scope["path"] == "/"
            await observed({"type": "http.response.start", "status": 200, "headers": []})
            await observed({"type": "http.response.body", "body": RESPONSE[:20], "more_body": True})
            if failure == "partial":
                raise RuntimeError("SDK interrupted")
            await observed({"type": "http.response.body", "body": RESPONSE[20:], "more_body": False})
            if failure == "final":
                raise RuntimeError("SDK interrupted")

        gateway = MCPGateway(self.store, cast("ChannelRuntimeManager", AsyncMock()), self.analytics)
        await gateway._dispatch(request, sdk, self.channel, send)


@pytest.fixture
async def delivery(tmp_path: Path) -> AsyncIterator[Delivery]:
    """Create real isolated SQLite persistence shared by both observation stores."""
    store = SaaSStore(f"sqlite:///{tmp_path / 'delivery.db'}")
    await store.initialize()
    try:
        tenant = await store.create_tenant("Delivery")
        channel = await store.create_channel(tenant.id, "Delivery")
        yield Delivery(store, AnalyticsStore(store._db), channel)
    finally:
        await store.close()


@dataclass
class Gate:
    """Pause one owned writer until explicit release, without sleeps or blocking the event loop."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        """Signal admission and retain observable cancellation cleanup."""
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.exited.set()


async def test_gateway_final_handoff_waits_for_both_committed_stores(delivery: Delivery) -> None:
    """An immediate usage/report query in ASGI send must see both writes, never eventual counts."""
    metric_gate, usage_gate = Gate(), Gate()
    original_metric, original_usage = delivery.analytics.record_request, delivery.store.record_usage

    async def metric(tenant: str, channel: str, value: RequestMetrics) -> bool:
        """Pause the real analytics writer before its transaction."""
        assert UUID(value.request_id).version == 4
        await metric_gate.wait()
        return await original_metric(tenant, channel, value)

    async def usage(tenant: str, channel: str, tool: str, status: Literal["success", "error"], ms: float) -> None:
        """Pause the legacy writer independently, after request analytics committed."""
        await usage_gate.wait()
        await original_usage(tenant, channel, tool, status, ms)

    with (
        patch.object(delivery.analytics, "record_request", side_effect=metric) as metrics,
        patch.object(delivery.store, "record_usage", side_effect=usage) as usages,
    ):
        task = asyncio.create_task(delivery.dispatch(delivery.send))
        try:
            async with asyncio.timeout(3):
                await metric_gate.entered.wait()
                assert not delivery.received.is_set() and not usage_gate.entered.is_set()
                assert not await delivery.store.list_usage(delivery.channel.tenant_id)
                metric_gate.release.set()
                await usage_gate.entered.wait()
                report = await delivery.analytics.get_report(delivery.channel.tenant_id)
                assert report["summary"]["requests"] == 1 and not delivery.received.is_set()
                usage_gate.release.set()
                await task
            assert delivery.received.is_set() and len(delivery.frames) == 3
            assert b"".join(frame.get("body", b"") for frame in delivery.frames) == RESPONSE
            metrics.assert_awaited_once()
            usages.assert_awaited_once()
        finally:
            metric_gate.release.set()
            usage_gate.release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["metric", "usage"])
async def test_gateway_cancellation_finishes_both_owned_writes(delivery: Delivery, phase: str) -> None:
    """Repeated cancellation cannot abandon either legacy or new persistence midway."""
    gate = Gate()
    target = delivery.analytics if phase == "metric" else delivery.store
    method = "record_request" if phase == "metric" else "record_usage"
    original = getattr(target, method)

    async def persist(*args: object) -> object:
        """Hold the selected write before delegating to its actual transaction."""
        await gate.wait()
        return await original(*args)

    with patch.object(target, method, side_effect=persist) as writer:
        task = asyncio.create_task(delivery.dispatch(delivery.send))
        try:
            async with asyncio.timeout(3):
                await gate.entered.wait()
                task.cancel()
                task.cancel()
                assert not delivery.received.is_set()
                gate.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            writer.assert_awaited_once()
            assert gate.exited.is_set() and not delivery.received.is_set()
            usage = await delivery.store.list_usage(delivery.channel.tenant_id)
            report = await delivery.analytics.get_report(delivery.channel.tenant_id)
            assert usage[0].calls == report["summary"]["requests"] == 1
        finally:
            gate.release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_gateway_both_writer_failures_are_bounded_nonfatal_and_static(delivery: Delivery, failure: str) -> None:
    """Optional storage faults must neither replace SDK bytes nor withhold final handoff indefinitely."""
    gate = Gate()
    sent = AsyncMock()

    async def persist(*args: object) -> None:
        """Raise private failure detail or stall until the production deadline cancels this writer."""
        if failure == "error":
            raise RuntimeError("private database detail")
        await gate.wait()

    with (
        patch.object(delivery.analytics, "record_request", side_effect=persist) as metrics,
        patch.object(delivery.store, "record_usage", side_effect=persist) as usages,
        patch("gryphon.saas_traffic._WRITE_TIMEOUT_SECONDS", 0.02),
        patch("gryphon.saas_gateway._USAGE_WRITE_TIMEOUT_SECONDS", 0.02),
        patch("gryphon.saas_traffic.logger") as metric_log,
        patch("gryphon.saas_gateway.logger") as usage_log,
    ):
        async with asyncio.timeout(1):
            await delivery.dispatch(sent)
        metrics.assert_awaited_once()
        usages.assert_awaited_once()
        metric_log.warning.assert_called_once_with("traffic_analytics_record_failed")
        usage_log.warning.assert_called_once_with("traffic_usage_record_failed")
    assert sent.await_count == 3 and sent.call_args.args[0]["more_body"] is False
    assert b"".join(call.args[0].get("body", b"") for call in sent.call_args_list) == RESPONSE
    if failure == "timeout":
        assert gate.entered.is_set() and gate.exited.is_set()


@pytest.mark.parametrize("failure", ["partial", "final"])
async def test_gateway_sdk_error_records_once_with_produced_completeness(delivery: Delivery, failure: str) -> None:
    """Fallback records an incomplete body once, and never duplicates a final-frame observation."""
    with (
        patch.object(delivery.analytics, "record_request", wraps=delivery.analytics.record_request) as metrics,
        patch.object(delivery.store, "record_usage", wraps=delivery.store.record_usage) as usages,
    ):
        with pytest.raises(RuntimeError, match="SDK interrupted"):
            await delivery.dispatch(AsyncMock(), failure=failure)
        metrics.assert_awaited_once()
        usages.assert_awaited_once()
        metric = metrics.call_args.args[2]
        assert metric.success is (failure == "final")
        assert metric.observation_complete is (failure == "final")
        assert metric.response_bytes == (20 if failure == "partial" else len(RESPONSE))
        assert metric.error_type == ("transport" if failure == "partial" else None)
    usage = await delivery.store.list_usage(delivery.channel.tenant_id)
    report = await delivery.analytics.get_report(delivery.channel.tenant_id)
    assert usage[0].calls == report["summary"]["requests"] == 1
    assert usage[0].status == ("error" if failure == "partial" else "success")


async def test_gateway_unknown_tool_never_records_request_or_usage(delivery: Delivery) -> None:
    """Unrecognized tool names remain outside both fixed-dimension observation stores."""
    sent = AsyncMock()
    with (
        patch.object(delivery.analytics, "record_request") as metrics,
        patch.object(delivery.store, "record_usage") as usages,
    ):
        await delivery.dispatch(sent, tool="unknown")
        metrics.assert_not_awaited()
        usages.assert_not_awaited()
    assert sent.await_count == 3
