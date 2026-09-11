"""POSIX executor ownership tests isolated to temporary run databases."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from gryphon.errors import CacheError
from gryphon.runtime.cache import _SQLiteStore
from gryphon.runtime.runs import RunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def stores(tmp_path: Path) -> AsyncIterator[tuple[RunStore, RunStore]]:
    """Yield two independent connections to one database and always close them."""
    first, second = RunStore(str(tmp_path / "runs.db")), RunStore(str(tmp_path / "runs.db"))
    await first.initialize()
    await second.initialize()
    try:
        yield first, second
    finally:
        await first.close()
        await second.close()


async def test_initialize_does_not_claim_executor(stores: tuple[RunStore, RunStore], tmp_path: Path) -> None:
    """Ledger clients can coexist without creating or acquiring the process lock."""
    assert not (tmp_path / "runs.db.lock").exists()
    first, second = stores
    receipt, _ = await first.create("alice", "digest", "key")
    repeated, new = await second.create("alice", "digest", "key")
    assert repeated.id == receipt.id and not new


async def test_second_store_claim_rejected_nonblocking(stores: tuple[RunStore, RunStore]) -> None:
    """A second executor fails promptly rather than sharing recovery ownership."""
    first, second = stores
    await first.claim_execution()
    with pytest.raises(CacheError, match="^Unable to claim executor ownership$"):
        await asyncio.wait_for(second.claim_execution(), timeout=1)
    assert second._execution_fd is None


async def test_repeated_claim_preserves_owned_descriptor(stores: tuple[RunStore, RunStore]) -> None:
    """Reclaiming through the same instance is idempotent and opens no new file."""
    first, _ = stores
    await first.claim_execution()
    descriptor = first._execution_fd
    with patch("gryphon.runtime.runs.os.open", side_effect=AssertionError("unexpected open")):
        await first.claim_execution()
    assert first._execution_fd == descriptor


async def test_failed_claim_close_does_not_release_other_owner(stores: tuple[RunStore, RunStore]) -> None:
    """A rejected executor's cleanup never closes the active executor's claim."""
    first, second = stores
    await first.claim_execution()
    with pytest.raises(CacheError):
        await second.claim_execution()
    await second.close()
    await second.close()
    await second.initialize()
    with pytest.raises(CacheError):
        await second.claim_execution()


async def test_close_releases_and_reacquires_same_lock_inode(stores: tuple[RunStore, RunStore], tmp_path: Path) -> None:
    """Ownership transfers without deleting or recreating the persistent lock file."""
    first, second = stores
    await first.claim_execution()
    lock_path = tmp_path / "runs.db.lock"
    inode = lock_path.stat().st_ino
    await first.close()
    await second.claim_execution()
    await first.close()
    await first.initialize()
    with pytest.raises(CacheError):
        await first.claim_execution()
    await second.close()
    await first.claim_execution()
    assert lock_path.stat().st_ino == inode


async def test_lock_descriptor_is_regular_private_and_not_inheritable(
    stores: tuple[RunStore, RunStore], tmp_path: Path
) -> None:
    """Claimed files have exact 0600 permissions and do not leak through exec."""
    first, _ = stores
    lock_path = tmp_path / "runs.db.lock"
    lock_path.write_text("persistent lock metadata")
    lock_path.chmod(0o644)
    await first.claim_execution()
    descriptor = first._execution_fd
    assert descriptor is not None
    metadata = os.fstat(descriptor)
    assert stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1 and not os.get_inheritable(descriptor)
    assert lock_path.read_text() == "persistent lock metadata"


async def test_lock_release_occurs_after_database_close(stores: tuple[RunStore, RunStore]) -> None:
    """Another executor remains excluded throughout pooled database closure."""
    first, second = stores
    await first.claim_execution()
    original_close = _SQLiteStore.close

    async def close_database() -> None:
        """Observe ownership before allowing the original database close."""
        with pytest.raises(CacheError):
            await second.claim_execution()
        await original_close(first)

    with patch.object(_SQLiteStore, "close", new=AsyncMock(side_effect=close_database)):
        await first.close()
    await second.claim_execution()
    assert second._execution_fd is not None


async def test_database_close_failure_still_releases_claim(stores: tuple[RunStore, RunStore]) -> None:
    """The finally path releases owned state even if the database close fails."""
    first, second = stores
    await first.claim_execution()
    with (
        patch.object(_SQLiteStore, "close", new=AsyncMock(side_effect=CacheError("Database unavailable"))),
        pytest.raises(CacheError, match="Database unavailable"),
    ):
        await first.close()
    assert first._execution_fd is None
    await second.claim_execution()


async def test_claim_before_initialize_rejected(tmp_path: Path) -> None:
    """An unopened ledger cannot claim execution ownership."""
    store = RunStore(str(tmp_path / "runs.db"))
    with pytest.raises(CacheError, match="before initialization"):
        await store.claim_execution()
    await store.close()
    assert not (tmp_path / "runs.db.lock").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
async def test_unsafe_lock_file_rejected(tmp_path: Path, kind: str) -> None:
    """Claiming never follows links or treats special files as ownership locks."""
    store = RunStore(str(tmp_path / "runs.db"))
    await store.initialize()
    lock_path, target = tmp_path / "runs.db.lock", tmp_path / "unrelated.txt"
    target.write_text("unchanged")
    if kind == "symlink":
        lock_path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, lock_path)
    elif kind == "directory":
        lock_path.mkdir()
    else:
        os.mkfifo(lock_path)
    try:
        with pytest.raises(CacheError) as caught:
            await asyncio.wait_for(store.claim_execution(), timeout=1)
        assert str(tmp_path) not in str(caught.value)
        assert store._execution_fd is None and target.read_text() == "unchanged"
    finally:
        await store.close()


async def test_failed_claim_permissions_cleanup_does_not_leak_descriptor(stores: tuple[RunStore, RunStore]) -> None:
    """A permission-setting failure closes the newly locked descriptor."""
    first, second = stores
    with (
        patch("gryphon.runtime.runs.os.fchmod", side_effect=OSError("private filesystem detail")),
        pytest.raises(CacheError, match="^Unable to claim executor ownership$"),
    ):
        await first.claim_execution()
    await second.claim_execution()
    assert first._execution_fd is None and second._execution_fd is not None


async def test_concurrent_claims_admit_only_one_executor(stores: tuple[RunStore, RunStore]) -> None:
    """Concurrent independent claimants have exactly one successful owner."""
    first, second = stores
    results = await asyncio.gather(first.claim_execution(), second.claim_execution(), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, CacheError) for result in results) == 1


def test_claim_exclusion_across_multiple_event_loops(tmp_path: Path) -> None:
    """Ownership is process-wide rather than confined to one asyncio event loop."""
    first, second = RunStore(str(tmp_path / "runs.db")), RunStore(str(tmp_path / "runs.db"))

    async def claim(store: RunStore) -> None:
        """Open one store and attempt ownership in the current event loop."""
        await store.initialize()
        await store.claim_execution()

    try:
        asyncio.run(claim(first))
        with pytest.raises(CacheError):
            asyncio.run(claim(second))
        asyncio.run(second.close())
        with pytest.raises(CacheError):
            asyncio.run(claim(second))
        asyncio.run(first.close())
        asyncio.run(second.claim_execution())
    finally:
        asyncio.run(first.close())
        asyncio.run(second.close())


async def test_claim_exclusion_across_processes(stores: tuple[RunStore, RunStore], tmp_path: Path) -> None:
    """A separate interpreter cannot claim the database until its owner closes."""
    first, _ = stores
    script = """
import asyncio
import sys
from gryphon.errors import CacheError
from gryphon.runtime.runs import RunStore

async def main() -> int:
    '''Report whether this independent interpreter can claim execution.'''
    store = RunStore(sys.argv[1])
    await store.initialize()
    try:
        await store.claim_execution()
        return 0
    except CacheError:
        return 23
    finally:
        await store.close()

raise SystemExit(asyncio.run(main()))
"""
    await first.claim_execution()
    arguments = [sys.executable, "-c", script, str(tmp_path / "runs.db")]
    held = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, timeout=10, check=False)
    assert held.returncode == 23, held.stderr.decode()
    await first.close()
    released = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, timeout=10, check=False)
    assert released.returncode == 0, released.stderr.decode()
