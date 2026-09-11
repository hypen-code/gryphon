"""Gryphon's bounded, durable execution pipeline with revocable capabilities.

Restricted Monty is the default. Docker is optional offline compute, never a
credential-bearing API proxy. Every submission and replay is guarded, inputs
are explicit JSON dictionaries, and accepted jobs receive owner-scoped receipts.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

from gryphon.errors import CacheError, CapacityError, ConflictError, ExecutionError
from gryphon.models import ExecutionResult, ExecutionScope, RunRecord
from gryphon.runtime.artifacts import ArtifactStore
from gryphon.runtime.execution_cleanup import acquire_slot, finish_cleanup
from gryphon.runtime.execution_results import bound_result, fingerprint
from gryphon.runtime.execution_results import failure as _failure
from gryphon.runtime.execution_validation import prepare_source, request_digest, validate_request
from gryphon.runtime.recovery_lease import RecoveryLease
from gryphon.runtime.runs import RunStore
from gryphon.runtime.sandboxes import DockerSandbox, RestrictedSandbox
from gryphon.security.ast_guard import ASTGuard
from gryphon.security.broker import ToolBroker
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.runtime.cache import CacheStore
    from gryphon.runtime.registry import Registry

logger = get_logger(__name__)
_DOCKER_MODULES = frozenset({"numpy", "pandas"})
_RESTRICTED_SECONDS = 30

# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


class CodeExecutor:
    """Own admission, execution, receipts, and cleanup for one server process."""

    def __init__(
        self,
        config: GryphonConfig,
        cache: CacheStore,
        registry: Registry,
        broker: ToolBroker | None = None,
        runs: RunStore | None = None,
    ) -> None:
        """Configure services without performing I/O.

        Args:
            config: Trusted server configuration.
            cache: Initialized recipe cache, owned by the caller.
            registry: Loaded authoritative capability catalog.
            broker: Host broker; constructed here when omitted, closed on shutdown.
            runs: Run ledger; this executor exclusively owns its recovery lifecycle.
        """
        self._config: GryphonConfig = config
        self._cache, self._registry = cache, registry
        self._broker = broker if broker is not None else ToolBroker(config, registry)
        limits = config.run_db_path, config.run_ttl_seconds, config.run_max_entries
        self._runs = runs if runs is not None else RunStore(*limits)
        self._lease = RecoveryLease(config.run_db_path)
        self.artifacts = ArtifactStore(config.artifact_dir, config.artifact_max_entries)
        self._sandbox = (
            DockerSandbox(config)
            if config.sandbox_mode == "docker"
            else RestrictedSandbox(config, registry, self._broker)
        )
        self._ast_guard = ASTGuard()
        self._semaphore = asyncio.Semaphore(config.max_concurrent_executions)
        self._admission = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task[ExecutionResult]] = {}
        self._scopes: dict[str, ExecutionScope] = {}
        self._keys: dict[tuple[str, str], tuple[str, str]] = {}
        self._reserved = 0
        self._started = False
        self._shutdown_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Acquire recovery ownership, initialize once, and start the backend."""
        async with self._admission:
            if self._shutdown_task is not None:
                raise ExecutionError("A stopped executor cannot be restarted")
            if self._started:
                return
            try:
                self._lease.acquire()
                await self._runs.initialize()
                await self._runs.recover_interrupted()
                await self._sandbox.startup()
            except BaseException:
                self._shutdown_task = asyncio.create_task(self._close_services())
                await finish_cleanup(self._shutdown_task)
                raise
            self._started = True
            logger.info(
                "executor_started",
                profile=self._config.sandbox_mode,
                concurrency=self._config.max_concurrent_executions,
            )

    async def shutdown(self) -> None:
        """Stop admission and finish cleanup despite repeated caller cancellation."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._drain())
        await finish_cleanup(self._shutdown_task)

    async def _drain(self) -> None:
        """Revoke and await only this executor's accepted jobs before service closure."""
        async with self._admission:
            self._started = False
            scopes, jobs = list(self._scopes.values()), list(self._jobs.values())
            for scope in scopes:
                scope.cancelled = True
            for job in jobs:
                job.cancel()
        try:
            await asyncio.gather(*jobs, return_exceptions=True)
            for scope in scopes:
                await self._finish_cancelled(scope.run_id, scope.owner)
        finally:
            await self._close_services()

    async def _close_services(self) -> None:
        """Close every service and release the recovery lease last, including on error."""
        try:
            await self._sandbox.close()
        finally:
            try:
                await self._broker.close()
            finally:
                try:
                    await self._runs.close()
                finally:
                    self._lease.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        code: str,
        description: str,
        inputs: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
        owner: str = "local",
        idempotency_key: str | None = None,
    ) -> ExecutionResult:
        """Submit and await one result without exposing raw traces.

        Args:
            code: Assign result, end with an expression, or explicitly return.
            description: Bounded recipe description, not logged.
            inputs: Explicit JSON dictionary, never interpolated into source.
            input_schema: Optional bounded input contract.
            owner: Trusted authentication-derived namespace.
            idempotency_key: Immutable request key within this owner.

        Returns:
            Terminal result and durable run identifier when admitted.
        """
        record: RunRecord | None = None
        try:
            record = await self.submit(code, description, inputs, input_schema, owner, idempotency_key)
            if record.result is not None:
                return record.result
            job = self._jobs.get(record.id)
            if job is not None:
                return (await asyncio.shield(job)).model_copy(deep=True)
            latest = await self._runs.get(record.id, owner)
            if latest is not None and latest.result is not None:
                return latest.result
            raise ExecutionError("Run has no active execution in this process")
        except asyncio.CancelledError:
            if record is not None:
                await self.cancel(record.id, owner)
            raise
        except Exception as exc:
            return _failure(exc, record.id if record else None)

    async def submit(
        self,
        code: str,
        description: str,
        inputs: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
        owner: str = "local",
        idempotency_key: str | None = None,
    ) -> RunRecord:
        """Validate before bounded admission; arguments have the execute contract.

        Returns:
            Existing or newly queued owner-scoped receipt.

        Raises:
            CapacityError: Active jobs and reservations reach twice concurrency.
            InputValidationError: A source or input boundary is invalid.
        """
        source, values, schema = self._validate(code, description, inputs, input_schema, owner, idempotency_key)
        identity = self._fingerprint()
        digest = request_digest(code, values, schema, identity, self._config.sandbox_mode)
        key = (owner, hashlib.sha256(idempotency_key.encode()).hexdigest()) if idempotency_key is not None else None
        if key is not None and key in self._keys:
            run_id, previous = self._keys[key]
            if previous != digest:
                raise ConflictError("Idempotency key belongs to a different request")
            record = await self._runs.get(run_id, owner)
            if record is not None:
                return record
        if len(self._jobs) + self._reserved >= self._config.max_concurrent_executions * 2:
            raise CapacityError("Execution admission capacity exhausted")
        self._reserved += 1
        try:
            async with self._admission:
                if not self._started or self._shutdown_task is not None:
                    raise ExecutionError("CodeExecutor.startup() has not completed")
                record, created = await self._runs.create(owner, digest, idempotency_key)
                if created:
                    self._launch(record, key, code, source, description, values, schema, identity)
                return record
        finally:
            self._reserved -= 1

    def _launch(
        self,
        record: RunRecord,
        key: tuple[str, str] | None,
        code: str,
        source: str,
        description: str,
        inputs: dict[str, Any],
        schema: dict[str, Any],
        identity: str,
    ) -> None:
        """Start exactly one job after the durable admission transaction commits."""
        scope = ExecutionScope(
            run_id=record.id,
            owner=record.owner,
            max_calls=self._config.max_tool_calls,
            deadline=time.monotonic() + self._config.queue_timeout_seconds,
        )
        self._scopes[record.id] = scope
        job = asyncio.create_task(self._work(record, scope, code, source, description, inputs, schema, identity))
        self._jobs[record.id] = job
        if key is not None:
            self._keys[key] = (record.id, record.request_hash)
        job.add_done_callback(lambda task: self._retire(record.id, key, task))

    async def replay(
        self, cache_id: str, inputs: dict[str, Any] | None = None, owner: str = "local"
    ) -> ExecutionResult:
        """Replay an owned current recipe with fresh inputs and the full guard pipeline."""
        try:
            entry = await self._cache.get(cache_id, owner=owner)
            if entry is None:
                raise CacheError("Cached recipe not found")
            if entry.swagger_hash != self._fingerprint():
                raise ConflictError("Cached recipe catalog or profile is stale")
            return await self.execute(entry.code, entry.description, inputs, entry.input_schema, owner)
        except Exception as exc:
            return _failure(exc)

    async def get_run(self, run_id: str, owner: str = "local") -> RunRecord | None:
        """Return only the requested owner's durable receipt, or None."""
        return await self._runs.get(run_id, owner)

    async def cancel(self, run_id: str, owner: str = "local") -> bool:
        """Revoke an owned active run and finish cancellation despite caller cancellation."""
        return await finish_cleanup(self._cancel(run_id, owner))

    async def _cancel(self, run_id: str, owner: str) -> bool:
        """Revoke broker grants before cancelling the job, then preserve terminal state."""
        record = await self._runs.get(run_id, owner)
        if record is None or record.status not in ("queued", "running"):
            return False
        scope = self._scopes.get(run_id)
        if scope is not None and scope.owner == owner:
            scope.cancelled = True
            job = self._jobs.get(run_id)
            if job is not None:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        await self._finish_cancelled(run_id, owner)
        return True

    def _validate(
        self,
        code: str,
        description: str,
        inputs: dict[str, Any] | None,
        schema: dict[str, Any] | None,
        owner: str,
        key: str | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Enforce source and input contracts before admission or VM compilation."""
        if not self._started or self._shutdown_task is not None:
            raise ExecutionError("CodeExecutor.startup() has not completed")
        # Enforce code size limit
        values, contract = validate_request(code, description, inputs, schema, owner, key, self._config)
        # Security scan
        self._guard(code)
        return prepare_source(code, self._config.sandbox_mode), values, contract

    def _guard(self, code: str) -> None:
        """Grant only the fixed numeric import profile, never caller-selected modules."""
        modules = _DOCKER_MODULES if self._config.sandbox_mode == "docker" else frozenset()
        self._ast_guard.validate(code, additional_allowed_modules=modules)

    async def _work(
        self,
        record: RunRecord,
        scope: ExecutionScope,
        code: str,
        source: str,
        description: str,
        inputs: dict[str, Any],
        schema: dict[str, Any],
        identity: str,
    ) -> ExecutionResult:
        """Bound queue wait, execute once, and durably persist a terminal receipt."""
        started, acquired, status = time.monotonic(), False, None
        try:
            await acquire_slot(self._semaphore, scope.deadline)
            acquired = True
            await self._runs.start(record.id, record.owner)
            if identity != self._fingerprint():
                raise ConflictError("Catalog or execution profile changed after admission")
            duration = self._config.execution_timeout_seconds
            duration = min(duration, _RESTRICTED_SECONDS) if self._config.sandbox_mode == "restricted" else duration
            scope.deadline = time.monotonic() + duration
            self._guard(code)
            async with asyncio.timeout_at(scope.deadline):
                result = await self._sandbox.run(source, inputs, scope)
                result.run_id, result.tool_calls = record.id, scope.calls
                result.execution_time_ms = int((time.monotonic() - started) * 1000)
                result = await bound_result(result, record.owner, self._config, self.artifacts)
                # Cache on success
                if result.success and self._config.cache_enabled:
                    result.cache_id = await self._cache.store(
                        code,
                        description,
                        sorted(server.name for server in self._registry.list_servers()),
                        identity,
                        owner=record.owner,
                        input_schema=schema,
                    )
        except asyncio.CancelledError:
            result = ExecutionResult(success=False, error="Execution cancelled", error_type="cancelled")
            status = "cancelled"
        except Exception as exc:
            result = _failure(exc, record.id)
        finally:
            scope.cancelled = True
            if acquired:
                self._semaphore.release()
        result.run_id, result.tool_calls = record.id, scope.calls
        await finish_cleanup(self._runs.finish(record.id, record.owner, result, status=status))
        return result

    async def _finish_cancelled(self, run_id: str, owner: str) -> None:
        """Persist cancellation even when a task was cancelled before its first step."""
        record = await self._runs.get(run_id, owner)
        if record is not None and record.status in ("queued", "running"):
            result = ExecutionResult(success=False, run_id=run_id, error="Execution cancelled", error_type="cancelled")
            try:
                await self._runs.finish(run_id, owner, result, status="cancelled")
            except ConflictError:
                logger.info("execution_already_finished", run_id=run_id)

    def _retire(self, run_id: str, key: tuple[str, str] | None, job: asyncio.Task[ExecutionResult]) -> None:
        """Release all per-job indexes and retrieve errors without leaking their text."""
        self._jobs.pop(run_id, None)
        self._scopes.pop(run_id, None)
        if key is not None:
            self._keys.pop(key, None)
        if not job.cancelled() and job.exception() is not None:
            logger.error("execution_receipt_failed", run_id=run_id)

    def _fingerprint(self) -> str:
        """Bind idempotency and replay to the full catalog and authority profile."""
        return fingerprint(self._config, self._registry)
