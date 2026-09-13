"""Persistent SSE, strict JSON-RPC identities, and bounded cancellation ownership."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from gryphon.errors import ExecutionError
from gryphon.security import mcp_client
from gryphon.security.mcp_client import discover_tools
from gryphon.security.mcp_protocol import SSEDecoder, parse_json, rpc_record, session_header
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig

URL = "https://shop.example.com/mcp"
RESPONSE = {"jsonrpc": "2.0", "id": "owned", "result": {"tools": []}}


async def _public(host: str, port: int) -> list[str]:
    """Use a deterministic public address without resolving a real host."""
    return ["93.184.216.34"]


def _event(record: Any) -> bytes:
    """Encode a single native JSON-RPC SSE message."""
    return b"event: message\ndata: " + json.dumps(record).encode() + b"\n\n"


class PersistentStream(httpx.AsyncByteStream):
    """Yield selected chunks, then remain open until cancelled or disconnected."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Retain controlled chunks and observable cleanup state."""
        self.chunks = chunks
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Do not close naturally after the matching response."""
        for chunk in self.chunks:
            yield chunk
        self.waiting.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        """Mark the transport body as locally disconnected."""
        self.closed = True


async def _read(config: GryphonConfig, stream: PersistentStream, *, limit: int = 10000) -> httpx.Response:
    """Drive the real pinned network reader without any upstream I/O."""
    client = NetworkClient(
        config,
        resolver=_public,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        ),
    )
    try:
        return await client.request("POST", URL, json_body={}, max_bytes=limit, timeout=0.2, rpc_response_id="owned")
    finally:
        await client.close()


async def test_mcp_client_sse_stops_before_eof(gryphon_config: GryphonConfig) -> None:
    notification = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "ignored"}}
    chunks = [b": heartbeat\n\n", _event(notification), _event(RESPONSE)]
    stream = PersistentStream(chunks)
    response = await _read(gryphon_config, stream)
    assert response.json() == RESPONSE
    assert response.extensions["mcp_consumed_bytes"] == sum(map(len, chunks))
    assert response.extensions["mcp_notifications"] == 1
    assert stream.closed and not stream.waiting.is_set()


@pytest.mark.parametrize("delimiter", [b"\n", b"\r", b"\r\n"])
async def test_mcp_client_sse_split_bytes(delimiter: bytes, gryphon_config: GryphonConfig) -> None:
    wire = _event(RESPONSE).replace(b"\n", delimiter)
    stream = PersistentStream([bytes([char]) for char in wire])
    assert (await _read(gryphon_config, stream)).json() == RESPONSE
    assert stream.closed


async def test_mcp_client_sse_multiline_data(gryphon_config: GryphonConfig) -> None:
    wire = b'data: {"jsonrpc":"2.0",\ndata: "id":"owned","result":{"tools":[]}}\n\n'
    assert (await _read(gryphon_config, PersistentStream([wire]))).json() == RESPONSE


@pytest.mark.parametrize(
    "wire",
    [
        _event({**RESPONSE, "id": "foreign"}),
        _event(RESPONSE) * 2,
        _event({"jsonrpc": "2.0", "method": "sampling/createMessage", "id": "server", "params": {}}),
        _event({"jsonrpc": "2.0", "id": "owned", "error": {"message": "private"}}),
        _event([RESPONSE]),
        b"data: not-json\n\n",
        b"event: other\ndata: {}\n\n",
        _event({"jsonrpc": "2.0", "method": "notifications/message"}) * 101,
    ],
)
async def test_mcp_client_sse_untrusted_records_fail(wire: bytes, gryphon_config: GryphonConfig) -> None:
    stream = PersistentStream([wire])
    with pytest.raises(ExecutionError) as error:
        await _read(gryphon_config, stream)
    assert "private" not in str(error.value)
    assert stream.closed


async def test_mcp_client_sse_bounded_before_parse(gryphon_config: GryphonConfig) -> None:
    stream = PersistentStream([b"data: " + b"x" * 1000])
    with pytest.raises(ExecutionError, match="size limit"):
        await _read(gryphon_config, stream, limit=100)
    assert stream.closed


async def test_mcp_client_sse_idle_deadline_closes(gryphon_config: GryphonConfig) -> None:
    stream = PersistentStream([b": keepalive\n\n"])
    with pytest.raises(ExecutionError, match="failed"):
        await _read(gryphon_config, stream)
    assert stream.closed


async def test_mcp_client_sse_cancel_closes(gryphon_config: GryphonConfig) -> None:
    stream = PersistentStream([b": keepalive\n\n"])
    task = asyncio.create_task(_read(gryphon_config, stream))
    await stream.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_mcp_client_sse_eager_response_and_eof(gryphon_config: GryphonConfig) -> None:
    client = NetworkClient(
        gryphon_config,
        resolver=_public,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_event(RESPONSE))
        ),
    )
    try:
        assert (await client.request("POST", URL, rpc_response_id="owned")).json() == RESPONSE
        with pytest.raises(ExecutionError, match="size limit"):
            await client.request("POST", URL, rpc_response_id="owned", max_bytes=1)
    finally:
        await client.close()


async def test_mcp_client_sse_eof_without_response(gryphon_config: GryphonConfig) -> None:
    client = NetworkClient(
        gryphon_config,
        resolver=_public,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, content=b": heartbeat\n\n"
            )
        ),
    )
    try:
        with pytest.raises(ExecutionError, match="without a response"):
            await client.request("POST", URL, rpc_response_id="owned")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'"\xff"',
        b"[[[",
        b"[" * 2000,
    ],
)
def test_mcp_client_strict_json_rejects(payload: bytes) -> None:
    with pytest.raises(ExecutionError, match="invalid JSON"):
        parse_json(payload)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {**RESPONSE, "jsonrpc": "1.0"},
        {**RESPONSE, "id": True},
        {**RESPONSE, "id": 1},
        {**RESPONSE, "error": {}},
        {"jsonrpc": "2.0", "id": "owned"},
        {**RESPONSE, "result": []},
        {"jsonrpc": "2.0", "method": "notifications/x", "params": []},
        {"jsonrpc": "2.0", "method": "notifications/x", "result": {}},
    ],
)
def test_mcp_client_record_identity_rejects(record: Any) -> None:
    with pytest.raises(ExecutionError):
        rpc_record(record, "owned")


@pytest.mark.parametrize("value", ["", "x" * 257, "contains space", "line\r\n", "tab\t", "\x7f"])
def test_mcp_client_session_header_rejects(value: str) -> None:
    with pytest.raises(ExecutionError, match="session identifier"):
        session_header(httpx.Headers({"Mcp-Session-Id": value}))


def test_mcp_client_session_duplicate_headers_rejects() -> None:
    with pytest.raises(ExecutionError, match="session identifier"):
        session_header(httpx.Headers([("Mcp-Session-Id", "one"), ("Mcp-Session-Id", "two")]))
    assert session_header(httpx.Headers()) is None
    assert session_header(httpx.Headers({"Mcp-Session-Id": "synthetic"})) == "synthetic"


def test_mcp_client_sse_priming_and_empty_lines() -> None:
    decoder = SSEDecoder("owned")
    decoder.feed(b"id: cursor\ndata:\n\ndata:\n\n")
    decoder.feed(_event(RESPONSE))
    assert decoder.result == RESPONSE


async def test_mcp_client_session_uses_persistent_sse(
    monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig
) -> None:
    streams: list[PersistentStream] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Answer every request over persistent SSE except the notification ACK."""
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, Any] = {"tools": []}
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
        stream = PersistentStream([_event({"jsonrpc": "2.0", "id": body["id"], "result": result})])
        streams.append(stream)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(mcp_client, "NetworkClient", lambda config: client)
    assert await discover_tools(gryphon_config, URL, 10000) == []
    assert len(streams) == 2 and all(stream.closed for stream in streams)
