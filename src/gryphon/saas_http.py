"""Shared hosted HTTP limits, trusted origins, and safe administration boundaries."""

from __future__ import annotations

import asyncio
import hmac
import json
from functools import wraps
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gryphon.errors import (
    CapacityError,
    CompileError,
    ConflictError,
    DockerUnavailableError,
    InputValidationError,
    SaaSDisabledError,
    SaaSNotFoundError,
    SaaSQuotaError,
    SaaSStoreError,
    SaaSValidationError,
    SecurityViolationError,
)
from gryphon.runtime.execution_validation import json_bytes
from gryphon.saas_auth import COOKIE_NAME, AdminSessions, RateLimiter
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from gryphon.saas_config import SaaSConfig

logger = get_logger(__name__)
_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def failure(category: str, status: int) -> JSONResponse:
    """Return a fixed public category without exception or input values."""
    return JSONResponse({"error": category}, status_code=status)


def _safe_failure(exc: Exception) -> JSONResponse:
    """Translate domain and unexpected failures without raw backend or validation diagnostics."""
    kinds: tuple[tuple[type[Exception], str, int], ...] = (
        (SaaSNotFoundError, "not_found", 404),
        (SaaSDisabledError, "disabled", 409),
        (SaaSQuotaError, "capacity", 429),
        (SaaSValidationError, "validation", 400),
        (SaaSStoreError, "storage_unavailable", 503),
        (DockerUnavailableError, "sandbox_unavailable", 503),
        (CapacityError, "capacity", 429),
        (ConflictError, "conflict", 409),
        (InputValidationError, "validation", 400),
        (CompileError, "validation", 400),
        (SecurityViolationError, "validation", 400),
        (ValueError, "validation", 400),
        (TimeoutError, "request_timeout", 408),
    )
    category, status = next(((kind, status) for cls, kind, status in kinds if isinstance(exc, cls)), ("internal", 500))
    logger.warning("hosted_request_failed", category=category)
    return failure(category, status)


async def read_object(request: Request, limit: int) -> dict[str, Any]:
    """Parse only bounded JSON objects with no non-finite numbers or deep structures."""
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise InputValidationError("JSON required")
    try:
        value = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise InputValidationError("Invalid JSON") from None
    if not isinstance(value, dict):
        raise InputValidationError("Object required")
    json_bytes(value, limit)
    return value


def fields(data: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    """Reject extra request properties rather than silently ignoring authority fields."""
    if not required <= data.keys() or data.keys() - required - (optional or set()):
        raise InputValidationError("Invalid fields")


def admin_endpoint(
    sessions: AdminSessions,
    handler: Callable[[Request], Awaitable[Response]],
    authorize: Callable[[Request], Awaitable[Response | None]] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """Authenticate before handler execution and require a session-bound CSRF token on writes."""

    @wraps(handler)
    async def guarded(request: Request) -> Response:
        """Use only server-verified session identity for the administration role."""
        csrf = sessions.verify(request.cookies.get(COOKIE_NAME, ""))
        if csrf is None:
            return failure("unauthorized", 401)
        if request.method in _MUTATIONS and not hmac.compare_digest(
            csrf.encode(), request.headers.get("x-csrf-token", "").encode()
        ):
            return failure("csrf", 403)
        if authorize is not None:
            denied = await authorize(request)
            if denied is not None:
                return denied
        return await handler(request)

    return guarded


class HTTPBoundary:
    """Bound admission and body production, reject DNS rebinding and cross-origin requests."""

    def __init__(self, app: ASGIApp, config: SaaSConfig) -> None:
        """Keep constant-memory service limits independent of unverified client identifiers."""
        self.app, self.config = app, config
        self.active = 0
        self.requests = RateLimiter(config.requests_per_minute)
        self.authority = urlsplit(config.public_origin).netloc.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check request framing before dispatch and attach defensive browser headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        secure_send = self._secure_send(send)
        error = self._preflight(request)
        if error is not None:
            await error(scope, receive, secure_send)
            return
        started = False

        async def tracked(message: Message) -> None:
            """Never attempt a second response if downstream delivery already began."""
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await secure_send(message)

        self.active += 1
        try:
            body = await self._body(request, receive)
            await self.app(scope, self._replay(body, receive), tracked)
        except Exception as exc:
            error = _safe_failure(exc)
            if not started:
                await error(scope, receive, secure_send)
            else:
                logger.error("hosted_response_interrupted")
        finally:
            self.active -= 1

    @staticmethod
    def _replay(body: bytes, receive: Receive) -> Receive:
        """Replay bounded request bytes once, then delegate disconnect observation."""
        consumed = False

        async def replay() -> Message:
            """Yield the complete bounded message once for downstream protocol handling."""
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    def _preflight(self, request: Request) -> Response | None:
        """Reject ambiguous framing, untrusted hosts, and browser-origin authority confusion."""
        for name in ("host", "origin", "authorization", "content-length"):
            if len(request.headers.getlist(name)) > 1:
                return failure("invalid_headers", 400)
        if request.headers.get("host", "").lower() != self.authority:
            return failure("invalid_host", 400)
        origin = request.headers.get("origin")
        if origin is not None and origin != self.config.public_origin:
            return failure("invalid_origin", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return failure("invalid_origin", 403)
        if self.active >= self.config.max_http_requests or not self.requests.accept():
            return failure("rate_limit", 429)
        if request.headers.get("content-encoding", "identity") != "identity":
            return failure("invalid_encoding", 415)
        return None

    async def _body(self, request: Request, receive: Receive) -> bytes:
        """Bound chunked bodies as well as declared lengths and slow upload duration."""
        limit = self.config.max_spec_bytes * 2 if request.url.path.endswith("/specs") else 1048576
        content_length = request.headers.get("content-length")
        if content_length is not None and (not content_length.isdigit() or int(content_length) > limit):
            raise InputValidationError("Request too large")
        body = bytearray()
        async with asyncio.timeout(self.config.request_timeout_seconds):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise InputValidationError("Disconnected request")
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    raise InputValidationError("Request too large")
                if not message.get("more_body", False):
                    return bytes(body)

    def _secure_send(self, send: Send) -> Send:
        """Prevent credential caching, script injection, framing and referrer leakage."""

        async def secure(message: Message) -> None:
            """Attach security headers without logging cookies or request bodies."""
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            b"default-src 'none'; script-src 'self'; style-src 'self'; "
                            b"connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                            b"frame-ancestors 'none'; form-action 'self'",
                        ),
                    ]
                )
                if self.config.secure_cookies:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": headers}
            await send(message)

        return secure
