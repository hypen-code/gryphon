"""Bounded, isolated hosted runtimes with request-scoped verified MCP ownership."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

from gryphon.errors import CapacityError, ConflictError, SecurityViolationError
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.executor import CodeExecutor
from gryphon.saas_catalog import (
    channel_config,
    compile_catalog,
    prepare_channel_storage,
    validate_id,
    validate_selection,
)
from gryphon.security.broker import ToolBroker
from gryphon.server import create_server
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from fastmcp.server.http import StarletteWithLifespan

    from gryphon.config import GryphonConfig
    from gryphon.models import Channel, SaaSSpec

verified_channel: ContextVar[str | None] = ContextVar("gryphon_verified_channel", default=None)
logger = get_logger(__name__)


class ChannelTokenVerifier(TokenVerifier):
    """Accept only channel identity verified by the enclosing database-authenticated app."""

    def __init__(self, channel_id: str) -> None:
        """Bind one canonical channel; request headers cannot choose its owner."""
        super().__init__()
        self.channel_id = validate_id(channel_id)
        self.active = True

    async def verify_token(self, token: str) -> AccessToken | None:
        """Convert trusted request context into FastMCP ownership, never parsing bearer claims."""
        if not self.active or verified_channel.get() != self.channel_id:
            return None
        return AccessToken(token=token, client_id=self.channel_id, scopes=[])


class _Runtime:
    """Own a single revision's dependencies and drain request references before closure."""

    def __init__(self, channel: Channel) -> None:
        """Track immutable configuration and initially idle, uninitialized resources."""
        self.channel = channel.model_copy(deep=True)
        self.verifier = ChannelTokenVerifier(channel.id)
        self.stack = AsyncExitStack()
        self.references = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.broker: ToolBroker | None = None
        self.app: StarletteWithLifespan
        self.closing: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._lifespan_task: asyncio.Task[None] | None = None

    async def start(self, config: GryphonConfig, specs: Sequence[SaaSSpec], execution_slots: asyncio.Semaphore) -> None:
        """Initialize every dependency with rollback registered before each fallible stage."""
        try:
            await finish_cleanup(asyncio.to_thread(prepare_channel_storage, config))
            registry = await compile_catalog(config, self.channel, specs)
            cache = CacheStore(config.cache_db_path, config.cache_ttl_seconds, config.cache_max_entries)
            self.stack.push_async_callback(cache.close)
            await cache.initialize()
            self.broker = ToolBroker(config, registry, allow_environment=False)
            self.stack.push_async_callback(self.broker.close)
            executor = CodeExecutor(config, cache, registry, broker=self.broker, execution_slots=execution_slots)
            self.stack.push_async_callback(executor.shutdown)
            await executor.startup()
            mcp = create_server(config, registry, cache, executor, auth=self.verifier)
            self.app = mcp.http_app(stateless_http=True, path="/", json_response=True)
            self._lifespan_task = asyncio.create_task(self._serve_app())
            self.stack.push_async_callback(self._stop_app)
            await asyncio.shield(self._ready)
        except BaseException:
            self.revoke()
            await finish_cleanup(self.stack.aclose())
            raise
        logger.info("channel_runtime_started", channel_id=self.channel.id, revision=self.channel.revision)

    async def _serve_app(self) -> None:
        """Enter and exit SDK task groups in the same dedicated lifecycle task."""
        try:
            async with self.app.router.lifespan_context(self.app):
                self._ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    async def _stop_app(self) -> None:
        """Signal the SDK lifespan owner instead of exiting its cancel scope in another task."""
        self._stop.set()
        if self._lifespan_task is not None:
            await self._lifespan_task

    def revoke(self) -> None:
        """Immediately reject new authentication and all further broker capability use."""
        self.verifier.active = False
        if self.broker is not None:
            self.broker.revoke()

    async def close(self) -> None:
        """Revoke synchronously, then close once all currently acquired requests have drained."""
        self.revoke()
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        await finish_cleanup(self.closing)

    async def _close(self) -> None:
        """Keep stores alive for ordinary requests, then cancel background jobs and close resources."""
        await self.idle.wait()
        await self.stack.aclose()
        logger.info("channel_runtime_closed", channel_id=self.channel.id, revision=self.channel.revision)


class ChannelRuntimeManager:
    """Keep bounded channel runtimes without evicting active background execution authority.

    The enclosing app must verify the tenant/channel key in its database on every
    request, set verified_channel to that channel's ID, and reset the context in
    a finally block. Invalidation requires a newer revision before reacquisition.
    The enclosing process owns the hosted database lease; executors additionally
    hold their ordinary exclusive per-channel run-ledger leases.
    """

    def __init__(self, base: GryphonConfig, state_dir: Path, max_runtimes: int) -> None:
        """Copy operator policy without reading environment or performing filesystem I/O."""
        if max_runtimes < 1:
            raise ValueError("Hosted runtime capacity must be positive")
        self._base = base.model_copy(deep=True)
        self._state_dir = state_dir
        self._maximum = max_runtimes
        self._execution_slots = asyncio.Semaphore(base.max_concurrent_executions)
        self._runtimes: dict[str, _Runtime] = {}
        self._revoked: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._closing: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def acquire(self, channel: Channel, specs: Sequence[SaaSSpec]) -> AsyncIterator[StarletteWithLifespan]:
        """Yield a ready stateless HTTP application while pinning its stores for the request."""
        snapshot = channel.model_copy(deep=True)
        validate_selection(snapshot, specs)
        async with self._lock:
            runtime = await self._acquire(snapshot, specs)
            runtime.references += 1
            runtime.idle.clear()
        try:
            yield runtime.app
        finally:
            runtime.references -= 1
            if runtime.references == 0:
                runtime.idle.set()

    async def _acquire(self, channel: Channel, specs: Sequence[SaaSSpec]) -> _Runtime:
        """Serialize initialization/replacement so no two executors can own a channel ledger."""
        if self._closed:
            raise SecurityViolationError("Hosted runtime manager is closed")
        if channel.revision <= self._revoked.get(channel.id, 0):
            raise ConflictError("Channel revision has been revoked")
        runtime = self._runtimes.get(channel.id)
        if runtime is not None:
            if channel.tenant_id != runtime.channel.tenant_id or channel.revision < runtime.channel.revision:
                raise ConflictError("Channel identity or revision is stale")
            if channel.revision == runtime.channel.revision:
                if channel != runtime.channel or not runtime.verifier.active:
                    raise ConflictError("Channel changed without a new revision")
                return runtime
            await self._retire(channel.id, runtime)
        if len(self._runtimes) >= self._maximum:
            raise CapacityError("Hosted runtime capacity exhausted")
        config = channel_config(self._base, channel, self._state_dir)
        runtime = _Runtime(channel)
        self._runtimes[channel.id] = runtime
        try:
            await runtime.start(config, specs, self._execution_slots)
            if self._closed or not runtime.verifier.active or channel.revision <= self._revoked.get(channel.id, 0):
                raise ConflictError("Channel authority changed during initialization")
        except BaseException:
            await runtime.close()
            self._runtimes.pop(channel.id, None)
            raise
        return runtime

    async def invalidate(self, channel_id: str, before_revision: int | None = None) -> None:
        """Revoke older authority without disabling a concurrently initialized committed revision.

        Args:
            channel_id: Canonical server-issued channel identity.
            before_revision: Newly committed revision; only strictly older revisions are revoked.
                None retains unconditional invalidation of the loaded or initializing runtime.
        """
        validate_id(channel_id)
        if before_revision is not None:
            if type(before_revision) is not int or before_revision < 1:
                raise ValueError("Invalid channel revision cutoff")
            self._revoked[channel_id] = max(self._revoked.get(channel_id, 0), before_revision - 1)
        runtime = self._runtimes.get(channel_id)
        if runtime is not None and (before_revision is None or runtime.channel.revision < before_revision):
            self._revoked[channel_id] = max(self._revoked.get(channel_id, 0), runtime.channel.revision)
            runtime.revoke()
        await finish_cleanup(self._invalidate(channel_id, before_revision))

    async def _invalidate(self, channel_id: str, before_revision: int | None) -> None:
        """Recheck the cutoff after queued initialization instead of retiring a newer replacement."""
        async with self._lock:
            runtime = self._runtimes.get(channel_id)
            if runtime is not None and (before_revision is None or runtime.channel.revision < before_revision):
                await self._retire(channel_id, runtime)

    async def _retire(self, channel_id: str, runtime: _Runtime) -> None:
        """Keep failed cleanup entries capacity-accounted and forbid stale resurrection."""
        self._revoked[channel_id] = max(self._revoked.get(channel_id, 0), runtime.channel.revision)
        await runtime.close()
        self._runtimes.pop(channel_id, None)

    async def close(self) -> None:
        """Revoke all channels before waiting, and finish cleanup despite caller cancellation."""
        self._closed = True
        for runtime in self._runtimes.values():
            runtime.revoke()
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await finish_cleanup(self._closing)

    async def _close(self) -> None:
        """Attempt every runtime's cleanup even when an individual dependency fails."""
        async with self._lock, AsyncExitStack() as stack:
            for channel_id, runtime in list(self._runtimes.items()):
                stack.push_async_callback(self._retire, channel_id, runtime)
