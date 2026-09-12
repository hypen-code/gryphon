"""Bounded JSON-RPC records and incremental POST response SSE decoding."""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any

import httpx

from gryphon.errors import ExecutionError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MAX_NOTIFICATIONS = 100
MAX_SESSION_ID_BYTES = 256
_LINE_END = re.compile(rb"\r\n|\r|\n")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON members rather than silently changing authority."""
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON member")
        result[name] = value
    return result


def _finite_float(value: str) -> float:
    """Reject overflow and nonstandard nonfinite JSON numbers."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid JSON number")
    return result


def parse_json(content: bytes) -> Any:
    """Decode strict finite UTF-8 JSON with sanitized failures."""
    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_finite_float,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutionError("Remote MCP returned invalid JSON") from None


def validate_acknowledgement(response: httpx.Response) -> None:
    """Accept empty 202/204 ACKs or the exact bounded JSON-object Shopify 200 ACK."""
    if response.status_code in (202, 204) and not response.content:
        return
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if response.status_code == 200 and media_type == "application/json" and parse_json(response.content) == {}:
        return
    raise ExecutionError("Remote MCP did not acknowledge initialization")


def normalize_content(content: list[dict[str, Any]]) -> Any:
    """Decode one unambiguous plain JSON text block; retain all other native data."""
    if len(content) != 1 or set(content[0]) != {"type", "text"}:
        return content
    block = content[0]
    if block["type"] != "text" or not isinstance(block["text"], str):
        return content
    try:
        return parse_json(block["text"].encode("utf-8"))
    except (ExecutionError, UnicodeError):
        return content


def rpc_record(value: Any, response_id: str) -> dict[str, Any] | None:
    """Accept only owned responses or inert notifications; never dispatch requests."""
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ExecutionError("Remote MCP returned an invalid protocol record")
    if "method" in value:
        method = value["method"]
        if (
            "id" in value
            or "result" in value
            or "error" in value
            or not isinstance(method, str)
            or not method.startswith("notifications/")
            or ("params" in value and not isinstance(value["params"], dict))
        ):
            raise ExecutionError("Remote MCP sent an unsupported request")
        return None
    if type(value.get("id")) is not str or value["id"] != response_id or ("result" in value) == ("error" in value):
        raise ExecutionError("Remote MCP returned an invalid response identity")
    if "error" in value:
        raise ExecutionError("Remote MCP request failed")
    if not isinstance(value["result"], dict):
        raise ExecutionError("Remote MCP returned an invalid result")
    return value


def session_header(headers: httpx.Headers) -> str | None:
    """Validate a single opaque session identifier without reflecting it."""
    values = headers.get_list("mcp-session-id")
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not 1 <= len(value) <= MAX_SESSION_ID_BYTES
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ExecutionError("Remote MCP returned an invalid session identifier")
    return value


class SSEDecoder:
    """Incrementally parse bounded SSE events without buffering until stream EOF."""

    def __init__(self, response_id: str) -> None:
        """Bind this parser to one host-generated JSON-RPC response identity."""
        self.response_id = response_id
        self.pending = bytearray()
        self.data: list[bytes] = []
        self.event = b""
        self.skip_lf = False
        self.notifications = 0
        self.result: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        """Process complete lines, including CRLF delimiters split across chunks."""
        if self.skip_lf and chunk:
            chunk = chunk.removeprefix(b"\n")
            self.skip_lf = False
        start = 0
        for match in _LINE_END.finditer(chunk):
            self.pending.extend(chunk[start : match.start()])
            self._line(bytes(self.pending))
            self.pending.clear()
            start = match.end()
            self.skip_lf = match.group() == b"\r" and start == len(chunk)
        self.pending.extend(chunk[start:])

    def _line(self, line: bytes) -> None:
        """Accumulate only event data; comments and transport IDs grant no authority."""
        if not line:
            self._event()
            self.data = []
            self.event = b""
            return
        field, separator, value = line.partition(b":")
        if separator:
            value = value.removeprefix(b" ")
        if field == b"data":
            self.data.append(value)
        elif field == b"event":
            self.event = value

    def _event(self) -> None:
        """Reject foreign and duplicate responses already received in this chunk."""
        if not self.data or not b"\n".join(self.data):
            return
        if self.event not in (b"", b"message"):
            raise ExecutionError("Remote MCP returned an unsupported SSE event")
        record = rpc_record(parse_json(b"\n".join(self.data)), self.response_id)
        if record is None:
            self.notifications += 1
            if self.notifications > MAX_NOTIFICATIONS:
                raise ExecutionError("Remote MCP notification limit exceeded")
        elif self.result is not None:
            raise ExecutionError("Remote MCP returned duplicate responses")
        else:
            self.result = record


async def consume_sse(response: httpx.Response, limit: int, response_id: str) -> httpx.Response:
    """Read bounded raw chunks, returning as soon as an owned response completes."""
    decoder = SSEDecoder(response_id)
    consumed = 0
    chunks = _single_chunk(response.content) if response.is_stream_consumed else response.aiter_raw()
    async for chunk in chunks:
        consumed += len(chunk)
        if consumed > limit:
            raise ExecutionError("Upstream response exceeds size limit")
        decoder.feed(chunk)
        if decoder.result is not None:
            headers = response.headers.copy()
            headers["Content-Type"] = "application/json"
            headers.pop("content-length", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                json=decoder.result,
                request=response.request,
                extensions={"mcp_consumed_bytes": consumed, "mcp_notifications": decoder.notifications},
            )
    raise ExecutionError("Remote MCP stream ended without a response")


async def _single_chunk(content: bytes) -> AsyncIterator[bytes]:
    """Adapt eagerly supplied test/transport bodies to the incremental decoder."""
    yield content
