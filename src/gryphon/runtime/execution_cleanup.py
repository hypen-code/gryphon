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
