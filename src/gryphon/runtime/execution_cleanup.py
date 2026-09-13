"""Cancellation-resistant cleanup for executor-owned asynchronous resources."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from gryphon.errors import CapacityError

if TYPE_CHECKING:
    from collections.abc import Awaitable


async def finish_cleanup[T](operation: Awaitable[T]) -> T:
    """Await owned cleanup to completion even if its caller is repeatedly cancelled.

    Args:
        operation: An owned cleanup operation or an already-running owned future.

    Returns:
        The operation's result, unless caller cancellation must be propagated.

    Raises:
        asyncio.CancelledError: After cleanup finishes if the caller was cancelled.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def acquire_slot(semaphore: asyncio.Semaphore, deadline: float) -> None:
    """Acquire bounded admission without confusing queue timeout with execution timeout.

    Args:
        semaphore: Executor-owned execution slots.
        deadline: Absolute monotonic queue deadline.

    Raises:
        CapacityError: No slot became available before the queue deadline.
    """
    try:
        async with asyncio.timeout_at(deadline):
            await semaphore.acquire()
    except TimeoutError:
        raise CapacityError("Execution queue wait exceeded limit") from None


class SlotLease:
    """Track one job's local and shared permits; release only acquired ownership."""

    def __init__(self, local: asyncio.Semaphore, shared: asyncio.Semaphore | None) -> None:
        """Remember per-channel and optional hosted capacity without acquiring it."""
        self._local, self._shared = local, shared
        self._local_acquired = self._shared_acquired = False

    async def acquire_local(self, deadline: float) -> None:
        """Acquire channel capacity; callers must release this lease in finally."""
        await acquire_slot(self._local, deadline)
        self._local_acquired = True

    async def acquire_shared(self, deadline: float) -> None:
        """Acquire hosted capacity under the same original queue deadline, if configured."""
        if self._shared is not None:
            await acquire_slot(self._shared, deadline)
            self._shared_acquired = True

    def release_shared(self) -> None:
        """Release hosted capacity once, after bounded execution and serialization."""
        if self._shared is not None and self._shared_acquired:
            self._shared.release()
            self._shared_acquired = False

    def release(self) -> None:
        """Release all acquired permits synchronously, including partial acquisition."""
        self.release_shared()
        if self._local_acquired:
            self._local.release()
            self._local_acquired = False
