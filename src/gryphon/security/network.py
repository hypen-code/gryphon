"""Validated, DNS-pinned, pooled upstream HTTP for broker and authentication traffic."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from gryphon.errors import ExecutionError, SecurityViolationError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.security.mcp_protocol import consume_sse
from gryphon.security.policies import check_address_allowed, check_domain_allowed, check_metadata_host, validated_url

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

Resolver = Callable[[str, int], Awaitable[list[str]]]
_READ_CHUNK_BYTES = 65536


async def resolve_addresses(hostname: str, port: int) -> list[str]:
    """Resolve TCP addresses without making an upstream connection.

    Args:
        hostname: Canonical hostname.
        port: Destination TCP port.

    Returns:
        All numeric addresses returned by DNS.
    """
    try:
        return [str(ipaddress.ip_address(hostname))]
    except ValueError:
        pass
    try:
        entries = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise SecurityViolationError("Upstream hostname resolution failed") from None
    return list(dict.fromkeys(str(entry[4][0]) for entry in entries))


class PinnedTransport(httpx.AsyncBaseTransport):
    """Pin DNS results while retaining HTTP Host and TLS certificate/SNI identity.

    Pools are isolated by original origin, not resolved IP, preventing connection
    reuse for a different TLS hostname sharing the same address.
    """

    def __init__(
        self,
        config: GryphonConfig,
        resolver: Resolver = resolve_addresses,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Accept only host-owned resolver/transport overrides, intended for tests."""
        self._config = config
        self._resolver = resolver
        self._override = transport
        self._pools: dict[tuple[str, str, int], httpx.AsyncBaseTransport] = {}
        self._closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Validate every DNS answer, requiring HTTPS except for private HTTP opt-in.

        Args:
            request: Host-built request, never a sandbox-supplied URL.

        Returns:
            Streaming response; validated IPv4 is preferred when available.
        """
        if self._closed:
            raise ExecutionError("Upstream network authority has been closed")
        url = validated_url(str(request.url))
        check_domain_allowed(str(url), self._config.allowed_domains)
        check_metadata_host(url.host)
        port = url.port or (443 if url.scheme == "https" else 80)
        addresses = await self._resolver(url.host, port)
        if self._closed:
            raise ExecutionError("Upstream network authority has been closed")
        if not addresses:
            raise SecurityViolationError("Upstream hostname resolution failed")
        for address in addresses:
            check_address_allowed(address, self._config.allow_private_networks, require_private=url.scheme == "http")
        address = min(addresses, key=lambda item: ipaddress.ip_address(item).version)
        key = (url.scheme, url.host, port)
        if key not in self._pools:
            self._pools[key] = self._override or httpx.AsyncHTTPTransport(
                verify=True,
                trust_env=False,
                retries=0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = url.host
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._pools[key].handle_async_request(pinned)

    async def aclose(self) -> None:
        """Revoke pending DNS work before closing all origin-specific connection pools."""
        self._closed = True
        pools = set(self._pools.values())
        self._pools.clear()
        for pool in pools:
            await pool.aclose()


class NetworkClient:
    """Broker-owned pooled client with bounded responses and sanitized failures."""

    def __init__(
        self,
        config: GryphonConfig,
        *,
        resolver: Resolver = resolve_addresses,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Configure verified TLS, no proxies/redirects, and suppress library URL logs."""
        self._config = config
        self._client = httpx.AsyncClient(
            verify=True,
            trust_env=False,
            follow_redirects=False,
            timeout=config.http_timeout_seconds,
            transport=PinnedTransport(config, resolver, transport),
        )
        names = {"httpx", "httpcore"} | {
            name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore.")
        }
        for name in names:
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        json_body_present: bool = False,
        data: Mapping[str, str | list[str]] | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        rpc_response_id: str | None = None,
    ) -> httpx.Response:
        """Send once and consume a hard-bounded, uncompressed response.

        Args:
            method: Validated method.
            url: Manifest or authentication configuration URL.
            headers: Host-owned request headers.
            json_body: Validated JSON payload.
            json_body_present: Preserve an explicit JSON null rather than omit the body.
            data: Validated URL-encoded form payload with scalar or repeated scalar values.
            content: Broker-encoded bounded multipart bytes, never a file or stream.
            timeout: Optional remaining scope budget.
            max_bytes: Optional response size limit, including compiler document limits.
            rpc_response_id: Opt-in owned MCP response identity for bounded SSE consumption.

        Returns:
            Fully read response; non-MCP error bodies are never returned. Native MCP
            non-2xx bodies stay internal to the protocol layer for classification.
        """
        if self._client.is_closed:
            raise ExecutionError("Upstream network authority has been closed")
        checked = validated_url(url)
        limit = max_bytes if max_bytes is not None else self._config.max_response_size_bytes
        duration = min(
            timeout if timeout is not None else self._config.http_timeout_seconds, self._config.http_timeout_seconds
        )
        if not math.isfinite(duration) or duration <= 0 or limit < 0:
            raise ExecutionError("Upstream request budget is invalid")
        request = self._build_request(
            method, checked, headers or {}, json_body, data, duration, json_body_present, content
        )
        return await self._send(request, duration, limit, rpc_response_id)

    def _build_request(
        self,
        method: str,
        url: httpx.URL,
        headers: dict[str, str],
        json_body: Any,
        data: Mapping[str, str | list[str]] | None,
        duration: float,
        json_body_present: bool,
        content: bytes | None = None,
    ) -> httpx.Request:
        """Preserve explicit null bodies and case-insensitive cookies without ambient state."""
        try:
            encodings = (json_body_present or json_body is not None, data is not None, content is not None)
            if len({name.lower() for name in headers}) != len(headers) or sum(encodings) > 1:
                raise ValueError("Ambiguous request encoding")
            if content is not None and not isinstance(content, bytes):
                raise ValueError("Raw request content must be broker-encoded bytes")
            explicit = httpx.Headers(headers)
            request_headers = httpx.Headers({"Accept": "application/json", "Accept-Encoding": "identity"})
            request_headers.update(explicit)
            if json_body_present and json_body is None:
                content = b"null"
                request_headers["Content-Type"] = "application/json"
            request = self._client.build_request(
                method,
                url,
                headers=request_headers,
                json=json_body,
                content=content,
                data=data,
                timeout=duration,
            )
            request.headers.pop("cookie", None)
            if "cookie" in explicit:
                request.headers["Cookie"] = explicit["cookie"]
            return request
        except (httpx.HTTPError, ValueError, TypeError):
            raise ExecutionError("Upstream request could not be encoded safely") from None

    async def _send(
        self, request: httpx.Request, duration: float, limit: int, rpc_response_id: str | None = None
    ) -> httpx.Response:
        """Apply a total deadline, bounded read and sanitized network failures."""
        try:
            async with asyncio.timeout(duration):
                response = await self._client.send(request, stream=True)
                try:
                    return await self._consume(response, limit, rpc_response_id)
                finally:
                    if rpc_response_id is not None:
                        await finish_cleanup(response.aclose())
                    else:
                        await response.aclose()
                    self._client.cookies.clear()
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            raise ExecutionError("Upstream request failed") from None

    async def _consume(
        self, response: httpx.Response, limit: int, rpc_response_id: str | None = None
    ) -> httpx.Response:
        """Reject HTTP failures, compression and size overflows without body disclosure.

        Native MCP responses keep their bounded non-2xx body so the protocol layer can
        classify a JSON-RPC error record instead of discarding it as a generic transport
        failure. The body is never returned to the sandbox, only reclassified upstream.
        """
        if rpc_response_id is None and not 200 <= response.status_code < 300:
            raise ExecutionError(f"Upstream returned HTTP {response.status_code}")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ExecutionError("Upstream compressed responses are not supported")
        length = response.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > limit):
            raise ExecutionError("Upstream response exceeds size limit")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if rpc_response_id is not None and media_type == "text/event-stream":
            return await consume_sse(response, limit, rpc_response_id)
        content = bytearray()
        if response.is_stream_consumed:
            if len(response.content) > limit:
                raise ExecutionError("Upstream response exceeds size limit")
            content.extend(response.content)
        else:
            async for chunk in response.aiter_raw(chunk_size=min(_READ_CHUNK_BYTES, limit + 1)):
                if len(content) + len(chunk) > limit:
                    raise ExecutionError("Upstream response exceeds size limit")
                content.extend(chunk)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(content),
            request=response.request,
        )

    async def close(self) -> None:
        """Release all pooled HTTP connections and cookies."""
        await self._client.aclose()
        self._client.cookies.clear()


def decode_json(response: httpx.Response) -> Any:
    """Decode finite JSON without allowing body values into parser errors.

    Args:
        response: Already bounded successful response.

    Returns:
        JSON-compatible value.
    """
    try:
        return json.loads(response.content, parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutionError("Upstream returned invalid JSON") from None


def _invalid_constant(value: str) -> None:
    """Reject nonstandard NaN and Infinity JSON values."""
    raise ValueError("Nonstandard JSON constant")


def _finite_float(value: str) -> float:
    """Reject valid numeric syntax whose magnitude would become infinity."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result
