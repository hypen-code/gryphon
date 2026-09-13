"""Shared hosted execution capacity across independently owned channel runtimes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gryphon.errors import CapacityError
from gryphon.models import ExecutionResult
from gryphon.runtime.execution_cleanup import SlotLease
from gryphon.runtime.executor import CodeExecutor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import Literal

    from gryphon.config import GryphonConfig
    from gryphon.models import ExecutionScope
    from gryphon.runtime.artifacts import ArtifactStore


class _ObservedSemaphore(asyncio.Semaphore):
    """Signal attempted shared admission without relying on arbitrary scheduling sleeps."""

    def __init__(self) -> None:
        """Start with one shared permit and a reusable admission signal."""
        super().__init__(1)
        self.attempted = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        """Signal before blocking on shared capacity."""
        self.attempted.set()
        return await super().acquire()


def _executor(config: GryphonConfig, path: Path, slots: asyncio.Semaphore | None) -> CodeExecutor:
    """Construct a real per-channel ledger with isolated backend and broker mocks."""
    config = config.model_copy(
        update={"run_db_path": str(path), "max_concurrent_executions": 1, "queue_timeout_seconds": 1}
    )
    registry, backend, cache = MagicMock(), AsyncMock(), AsyncMock()
    registry.fingerprint.return_value = "catalog"
    registry.list_servers.return_value = []
    backend.run.return_value = ExecutionResult(success=True, data=42)
    cache.store.return_value = "recipe"
    with patch("gryphon.runtime.executor.RestrictedSandbox", return_value=backend):
        return CodeExecutor(config, cache, registry, AsyncMock(), execution_slots=slots)


@pytest.fixture
async def channels(
    gryphon_config: GryphonConfig, tmp_path: Path
) -> AsyncIterator[tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore]]:
    """Own two independent executors sharing exactly one hosted execution permit."""
    slots = _ObservedSemaphore()
    first = _executor(gryphon_config, tmp_path / "first.db", slots)
    second = _executor(gryphon_config, tmp_path / "second.db", slots)
    try:
        await first.startup()
        await second.startup()
        yield first, second, slots
    finally:
        await asyncio.gather(first.shutdown(), second.shutdown())


def _block(executor: CodeExecutor) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold a backend execution until explicitly released or cancelled."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(code: str, inputs: dict[str, object], scope: ExecutionScope) -> ExecutionResult:
        """Expose backend entry while retaining its shared permit."""
        entered.set()
        await release.wait()
        return ExecutionResult(success=True, data=42)

    cast("AsyncMock", executor._sandbox.run).side_effect = run
    return entered, release


async def _wait(event: asyncio.Event) -> None:
    """Bound synchronization so a regression fails rather than hanging the suite."""
    await asyncio.wait_for(event.wait(), timeout=3)


async def test_hosted_admission_independent_channels_cannot_execute_simultaneously(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """Channel-local capacity must not multiply the shared VM concurrency limit."""
    first, second, slots = channels
    first_entered, first_release = _block(first)
    second_entered, second_release = _block(second)
    first_record = await first.submit("result = 42", "first")
    await _wait(first_entered)
    slots.attempted.clear()
    second_record = await second.submit("result = 42", "second")
    await _wait(slots.attempted)
    assert not second_entered.is_set() and slots.locked()
    first_release.set()
    await _wait(second_entered)
    assert slots.locked()
    second_release.set()
    await asyncio.gather(*first._jobs.values(), *second._jobs.values())
    assert not slots.locked()
    first_receipt, second_receipt = await first.get_run(first_record.id), await second.get_run(second_record.id)
    assert first_receipt is not None and first_receipt.status == "succeeded"
    assert second_receipt is not None and second_receipt.status == "succeeded"


async def test_hosted_admission_cancelled_waiter_cannot_steal_or_leak_permits(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """Cancelling a channel waiting globally releases only its own local slot."""
    first, second, slots = channels
    entered, release = _block(first)
    await first.submit("result = 42", "holder")
    await _wait(entered)
    slots.attempted.clear()
    queued = await second.submit("result = 42", "waiter")
    await _wait(slots.attempted)
    assert await second.cancel(queued.id)
    assert slots.locked() and not second._semaphore.locked()
    cast("AsyncMock", second._sandbox.run).assert_not_awaited()
    release.set()
    await asyncio.gather(*first._jobs.values())
    assert (await second.execute("result = 42", "retry")).success
    async with asyncio.timeout(1):
        await slots.acquire()
    assert slots.locked()
    slots.release()


async def test_hosted_admission_cancellation_waits_for_backend_cleanup_before_release(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """Another channel cannot start while a cancelled VM is still being cleaned up."""
    first, second, slots = channels
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run(code: str, inputs: dict[str, object], scope: ExecutionScope) -> ExecutionResult:
        """Model a backend that waits for owned worker cleanup on cancellation."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
        return ExecutionResult(success=True)

    cast("AsyncMock", first._sandbox.run).side_effect = run
    holder = await first.submit("result = 42", "holder")
    await _wait(entered)
    slots.attempted.clear()
    await second.submit("result = 42", "waiter")
    await _wait(slots.attempted)
    cancelling = asyncio.create_task(first.cancel(holder.id))
    try:
        await _wait(cleanup)
        assert slots.locked() and not cancelling.done()
        cast("AsyncMock", second._sandbox.run).assert_not_awaited()
    finally:
        release.set()
        await cancelling
    await asyncio.gather(*second._jobs.values())
    cast("AsyncMock", second._sandbox.run).assert_awaited_once()
    assert not slots.locked()


async def test_hosted_admission_global_queue_timeout_returns_safe_capacity_receipt(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """A global admission timeout is capacity, not an execution timeout or raw error."""
    first, second, slots = channels
    entered, release = _block(first)
    await first.submit("result = 42", "holder")
    await _wait(entered)
    result = await second.execute("result = 42", "queued")
    assert not result.success and result.error_type == "capacity" and result.run_id is not None
    record = await second.get_run(result.run_id)
    assert record is not None and record.status == "failed"
    assert result.error == "Execution capacity or resource budget exceeded"
    assert slots.locked() and not second._semaphore.locked()
    cast("AsyncMock", second._sandbox.run).assert_not_awaited()
    release.set()
    await asyncio.gather(*first._jobs.values())
    assert (await second.execute("result = 42", "retry")).success


async def test_hosted_admission_channel_job_queue_remains_bounded(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """Shared waiting never bypasses each channel's existing two-job bound."""
    first, second, slots = channels
    entered, _ = _block(first)
    await first.submit("result = 42", "holder")
    await _wait(entered)
    slots.attempted.clear()
    await second.submit("result = 42", "global wait")
    await _wait(slots.attempted)
    await second.submit("result = 42", "local wait")
    with pytest.raises(CapacityError):
        await second.submit("result = 42", "rejected")
    await second.shutdown()
    assert slots.locked() and not second._semaphore.locked()


async def test_hosted_admission_serialization_retains_shared_capacity(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """Backend result memory stays bounded until result serialization completes."""
    first, second, slots = channels
    serializing, release = asyncio.Event(), asyncio.Event()

    async def bound(
        result: ExecutionResult, owner: str, config: GryphonConfig, artifacts: ArtifactStore
    ) -> ExecutionResult:
        """Hold the first channel's bounded result production phase."""
        if config is first._config:
            serializing.set()
            await release.wait()
        return result

    with patch("gryphon.runtime.executor.bound_result", side_effect=bound):
        await first.submit("result = 42", "serialize")
        try:
            await _wait(serializing)
            slots.attempted.clear()
            await second.submit("result = 42", "waiter")
            await _wait(slots.attempted)
            assert slots.locked()
            cast("AsyncMock", second._sandbox.run).assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(*first._jobs.values(), *second._jobs.values())
    assert not slots.locked()


@pytest.mark.parametrize("stage", ["cache", "receipt"])
async def test_hosted_admission_persistence_does_not_hold_global_slot(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore], stage: str
) -> None:
    """Slow recipe/receipt persistence does not reserve hosted VM capacity."""
    first, second, slots = channels
    entered, release = asyncio.Event(), asyncio.Event()
    target = first._cache if stage == "cache" else first._runs
    method = "store" if stage == "cache" else "finish"
    original = getattr(target, method)

    async def persist(*args: object, **kwargs: object) -> object:
        """Delay the selected real/mocked persistence method until assertions finish."""
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    with patch.object(target, method, side_effect=persist):
        await first.submit("result = 42", "persist")
        try:
            await _wait(entered)
            assert not slots.locked()
            result = await second.execute("result = 42", "independent")
            assert result.success
        finally:
            release.set()
            await asyncio.gather(*first._jobs.values())
    assert not slots.locked()


async def test_hosted_admission_failure_releases_both_owned_permits(
    channels: tuple[CodeExecutor, CodeExecutor, _ObservedSemaphore],
) -> None:
    """An unexpected backend failure cannot strand shared or local capacity."""
    first, second, slots = channels
    cast("AsyncMock", first._sandbox.run).side_effect = RuntimeError("private failure")
    result = await first.execute("result = 42", "failure")
    assert result.error_type == "internal" and "private" not in result.model_dump_json()
    assert not slots.locked() and not first._semaphore.locked()
    assert (await second.execute("result = 42", "retry")).success


async def test_hosted_admission_default_executors_do_not_share_capacity(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
) -> None:
    """Omitting the optional shared semaphore preserves independent stdio execution."""
    first = _executor(gryphon_config, tmp_path / "first.db", None)
    second = _executor(gryphon_config, tmp_path / "second.db", None)
    entered, _ = _block(first)
    try:
        await first.startup()
        await second.startup()
        await first.submit("result = 42", "stdio first")
        await _wait(entered)
        assert (await second.execute("result = 42", "stdio second")).success
    finally:
        await asyncio.gather(first.shutdown(), second.shutdown())


async def test_hosted_admission_local_queue_timeout_never_releases_unowned_shared_slot() -> None:
    """Partial acquisition cleanup is safe even before the local permit is acquired."""
    local, shared = asyncio.Semaphore(0), asyncio.Semaphore(0)
    lease = SlotLease(local, shared)
    try:
        with pytest.raises(CapacityError):
            await lease.acquire_local(time.monotonic() + 0.01)
    finally:
        lease.release()
        lease.release()
    assert local.locked() and shared.locked()


async def test_hosted_admission_shared_wait_uses_original_queue_deadline() -> None:
    """Waiting locally must not grant a fresh hosted queue timeout budget."""
    local, shared = asyncio.Semaphore(1), asyncio.Semaphore(0)
    lease = SlotLease(local, shared)
    deadline = time.monotonic() + 0.01
    await lease.acquire_local(deadline)
    await asyncio.sleep(0.02)
    try:
        with pytest.raises(CapacityError):
            await lease.acquire_shared(deadline)
    finally:
        lease.release()
    assert not local.locked() and shared.locked()
