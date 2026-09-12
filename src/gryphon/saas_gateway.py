"""Authenticate every channel request before routing into a separately owned MCP runtime."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from uuid import UUID

from starlette.requests import Request

from gryphon.errors import CapacityError, ConflictError, SecurityViolationError
from gryphon.saas_http import failure
from gryphon.saas_runtime import ChannelRuntimeManager, verified_channel
from gryphon.saas_store import TOOLS, SaaSStore
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from gryphon.models import Channel

logger = get_logger(__name__)


def _tool(body: bytes) -> str | None:
    """Recognize only a fixed tool name for telemetry; discard all other request data."""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("method") == "tools/call":
            params = data.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            return name if isinstance(name, str) and name in TOOLS else None
    except (ValueError, RecursionError):
        pass
    return None


class MCPGateway:
    """Resolve opaque channel credentials from the database, never from URL claims."""

    def __init__(self, store: SaaSStore, runtimes: ChannelRuntimeManager) -> None:
        """Inject independently managed persistence and runtime dependencies."""
        self.store, self.runtimes = store, runtimes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authorize the exact path and revision, then delegate protocol handling to FastMCP."""
        request = Request(scope, receive)
        channel_id = scope["path"].rstrip("/").rsplit("/", 1)[-1]
        header = request.headers.get("authorization", "")
        try:
            valid_id = str(UUID(channel_id)) == channel_id
        except ValueError:
            valid_id = False
        identity = await self.store.lookup_key(header[7:]) if header.startswith("Bearer ") and valid_id else None
        if identity is None or identity[1].id != channel_id:
            await failure("unauthorized", 401)(scope, receive, send)
            return
        channel = identity[1]
        specs = [await self.store.get_spec(channel.tenant_id, spec_id) for spec_id in channel.spec_ids]
        try:
            async with self.runtimes.acquire(channel, specs) as app:
                current = await self.store.lookup_key(header[7:])
                if current is None or current[1] != channel:
                    await failure("unauthorized", 401)(scope, receive, send)
                    return
                token = verified_channel.set(channel.id)
                try:
                    await self._dispatch(request, app, channel, send)
                finally:
                    verified_channel.reset(token)
        except CapacityError:
            await failure("capacity", 429)(scope, receive, send)
        except (ConflictError, SecurityViolationError):
            await failure("channel_unavailable", 409)(scope, receive, send)

    async def _dispatch(self, request: Request, app: ASGIApp, channel: Channel, send: Send) -> None:
        """Record fixed tool outcomes after SDK dispatch without retaining request values."""
        body = await request.body()
        tool, started = _tool(body), time.monotonic()
        status = 500
        response = bytearray()

        async def observed(message: Message) -> None:
            """Inspect bounded response bytes only for result status and never log them."""
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and len(response) < 1048576:
                response.extend(message.get("body", b"")[: 1048576 - len(response)])
            await send(message)

        delivered = False

        async def replay() -> Message:
            """Provide the already bounded request once, preserving disconnect delivery."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await request.receive()

        scope = {**request.scope, "path": "/", "raw_path": b"/", "root_path": ""}
        await app(scope, replay, observed)
        if tool is not None:
            await self.store.record_usage(
                channel.tenant_id,
                channel.id,
                tool,
                "success" if _successful(status, response) else "error",
                (time.monotonic() - started) * 1000,
            )


def _successful(status: int, body: bytearray) -> bool:
    """Distinguish MCP errors and structured execution failures from successful HTTP transport."""
    if not 200 <= status < 300:
        return False
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or "error" in data:
            return False
        result = data.get("result", {})
        if not isinstance(result, dict) or result.get("isError"):
            return False
        structured = result.get("structuredContent", {})
        return not isinstance(structured, dict) or structured.get("success") is not False
    except (ValueError, RecursionError):
        return False
