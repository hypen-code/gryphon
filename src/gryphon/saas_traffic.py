"""Bounded scalar MCP response observations without retaining request or result content."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, cast, get_args
from uuid import uuid4

from gryphon.models.analytics import RunErrorType
from gryphon.models.traffic import RequestErrorType, RequestMetrics, ToolName
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import Message

    from gryphon.saas_analytics import AnalyticsStore

_RESPONSE_LIMIT = 1048576
_WRITE_TIMEOUT_SECONDS = 2
_SAFE_ERRORS = frozenset(get_args(RunErrorType.__value__)) | {"protocol", "transport", "unknown", "cache_miss", "stale"}
logger = get_logger(__name__)


class TrafficObservation:
    """Track response production and parse one bounded structured result before final handoff."""

    def __init__(self, request_bytes: int) -> None:
        """Start a server-identified observation independent of caller-supplied JSON-RPC IDs."""
        self.request_id = uuid4().hex
        self.request_bytes = request_bytes
        self.response_bytes = 0
        self.status = 500
        self.started = time.monotonic()
        self.buffer = bytearray()
        self.finished = False
        self.truncated = False

    def observe(self, message: Message) -> None:
        """Count every produced body byte while retaining no more than the fixed parse budget."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            self.response_bytes += len(body)
            self.buffer.extend(body[: max(0, _RESPONSE_LIMIT - len(self.buffer))])
            self.truncated = self.response_bytes > _RESPONSE_LIMIT
            self.finished = not message.get("more_body", False)

    def snapshot(self, tool: str, success: bool) -> RequestMetrics:
        """Extract static status and canonical payload size, then immediately erase captured bytes."""
        payload_bytes = 0
        error: RequestErrorType | None = None if success else "protocol"
        complete = self.finished and not self.truncated
        if not self.finished:
            error = "transport"
        try:
            data = json.loads(self.buffer)
            result = data.get("result") if isinstance(data, dict) else None
            payload = result.get("structuredContent") if isinstance(result, dict) else None
            if isinstance(payload, dict):
                payload_bytes = len(json_bytes(payload, _RESPONSE_LIMIT))
                category = payload.get("error_type")
                if not success and isinstance(category, str):
                    error = cast("RequestErrorType", category if category in _SAFE_ERRORS else "unknown")
        except Exception:
            complete = False
            logger.warning("traffic_analytics_measurement_incomplete")
        finally:
            self.buffer.clear()
        return RequestMetrics(
            request_id=self.request_id,
            tool=cast("ToolName", tool),
            success=success,
            error_type=error,
            request_bytes=self.request_bytes,
            response_bytes=self.response_bytes,
            payload_bytes=payload_bytes,
            duration_ms=(time.monotonic() - self.started) * 1000,
            observation_complete=complete,
        )


async def record_traffic(
    store: AnalyticsStore, tenant_id: str, channel_id: str, observation: TrafficObservation, tool: str, success: bool
) -> None:
    """Bound and finish optional persistence without replacing the produced MCP response."""
    try:
        metric = observation.snapshot(tool, success)
        await finish_cleanup(_write(store, tenant_id, channel_id, metric))
    except Exception:
        logger.warning("traffic_analytics_record_failed")


async def _write(store: AnalyticsStore, tenant_id: str, channel_id: str, metric: RequestMetrics) -> None:
    """Limit optional telemetry persistence independently of execution or transport deadlines."""
    async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
        await store.record_request(tenant_id, channel_id, metric)
