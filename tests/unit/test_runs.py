"""Isolated durability, idempotency, and transition tests for run receipts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from gryphon.errors import CacheError, ConflictError, InputValidationError
from gryphon.models import ExecutionResult
from gryphon.runtime.runs import RunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def runs(tmp_path: Path) -> AsyncIterator[RunStore]:
    """Yield an isolated ledger and always close its pooled connection."""
    store = RunStore(str(tmp_path / "runs.db"), ttl_seconds=100, max_entries=3)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def test_run_create_start_finish_is_durable(runs: RunStore) -> None:
    """Each committed transition and public result survives reopening."""
    record, new = await runs.create("alice", "digest")
    assert new and record.status == "queued"
    await runs.start(record.id, "alice")
    await runs.finish(record.id, "alice", ExecutionResult(success=True, data={"value": 42}))
    await runs.close()
    await runs.initialize()
    fetched = await runs.get(record.id, "alice")
    assert fetched is not None and fetched.status == "succeeded"
    assert fetched.result is not None and fetched.result.run_id == record.id
    assert fetched.result.data == {"value": 42}


async def test_run_idempotency_concurrent_duplicates_are_atomic(runs: RunStore) -> None:
    """Concurrent identical submissions claim exactly one receipt."""
    claims = await asyncio.gather(*(runs.create("alice", "digest", "key") for _ in range(25)))
    assert len({record.id for record, _ in claims}) == 1
    assert sum(new for _, new in claims) == 1


async def test_run_idempotency_two_connections_are_atomic(tmp_path: Path) -> None:
    """SQLite serializes ownership claims across independent pooled connections."""
    first, second = RunStore(str(tmp_path / "shared.db")), RunStore(str(tmp_path / "shared.db"))
    await first.initialize()
    await second.initialize()
    try:
        claims = await asyncio.gather(first.create("alice", "digest", "key"), second.create("alice", "digest", "key"))
        assert claims[0][0].id == claims[1][0].id
        assert sum(new for _, new in claims) == 1
    finally:
        await first.close()
        await second.close()


async def test_run_idempotency_changed_request_conflicts_without_details(runs: RunStore) -> None:
    """Retained keys cannot be reused for different request identities."""
    await runs.create("alice", "first digest", "private caller key")
    with pytest.raises(ConflictError, match="^Idempotency key belongs to a different request$"):
        await runs.create("alice", "second digest", "private caller key")


async def test_run_namespace_keys_and_access_are_isolated(runs: RunStore) -> None:
    """Equal caller keys in separate namespaces never alias or grant access."""
    alice, _ = await runs.create("alice", "digest", "key")
    bob, new = await runs.create("bob", "digest", "key")
    assert new and alice.id != bob.id
    assert await runs.get(alice.id, "bob") is None
    with pytest.raises(ConflictError):
        await runs.start(alice.id, "bob")
    with pytest.raises(ConflictError):
        await runs.finish(alice.id, "bob", ExecutionResult(success=True))


async def test_run_terminal_receipt_is_not_overwritten(runs: RunStore) -> None:
    """A late worker cannot overwrite cancellation or restart a terminal run."""
    record, _ = await runs.create("alice", "digest", "key")
    await runs.finish(record.id, "alice", ExecutionResult(success=False), status="cancelled")
    with pytest.raises(ConflictError):
        await runs.finish(record.id, "alice", ExecutionResult(success=True))
    with pytest.raises(ConflictError):
        await runs.start(record.id, "alice")
    fetched, new = await runs.create("alice", "digest", "key")
    assert not new and fetched.status == "cancelled"


async def test_run_start_cannot_start_same_receipt_twice(runs: RunStore) -> None:
    """Only the first queued-to-running claimant can start execution."""
    record, _ = await runs.create("alice", "digest")
    await runs.start(record.id, "alice")
    with pytest.raises(ConflictError):
        await runs.start(record.id, "alice")


async def test_run_initialize_does_not_recover_active_runs(runs: RunStore) -> None:
    """Recovery is exclusively owned by executor startup, not DB initialization."""
    record, _ = await runs.create("alice", "digest")
    await runs.start(record.id, "alice")
    await runs.close()
    await runs.initialize()
    fetched = await runs.get(record.id, "alice")
    assert fetched is not None and fetched.status == "running"


async def test_run_recovery_marks_queued_running_only_without_replay(runs: RunStore) -> None:
    """Both abandoned states become interrupted while terminal results survive."""
    queued, _ = await runs.create("alice", "queued", "queued")
    running, _ = await runs.create("alice", "running", "running")
    terminal, _ = await runs.create("alice", "terminal")
    await runs.start(running.id, "alice")
    await runs.finish(terminal.id, "alice", ExecutionResult(success=True))
    assert await runs.recover_interrupted() == 2
    assert await runs.recover_interrupted() == 0
    for record in (queued, running):
        fetched = await runs.get(record.id, "alice")
        assert fetched is not None and fetched.status == "interrupted"
        assert fetched.result is not None and fetched.result.error_type == "interrupted"
    fetched, new = await runs.create("alice", "queued", "queued")
    assert not new and fetched.id == queued.id and fetched.status == "interrupted"


async def test_run_capacity_never_evicts_active_receipts(runs: RunStore) -> None:
    """A full ledger rejects admission instead of releasing active claims."""
    for index in range(3):
        await runs.create("alice", str(index))
    with pytest.raises(CacheError, match="capacity"):
        await runs.create("bob", "new")


async def test_run_capacity_evicts_oldest_terminal_receipt(runs: RunStore) -> None:
    """Terminal receipt retention keeps the table bounded on admission."""
    first, _ = await runs.create("alice", "first")
    await runs.finish(first.id, "alice", ExecutionResult(success=True))
    for index in range(3):
        await runs.create("alice", str(index))
    assert await runs.get(first.id, "alice") is None


async def test_run_ttl_expires_terminal_but_not_active_claims(runs: RunStore) -> None:
    """Expiration releases terminal idempotency but never active work."""
    with patch("gryphon.runtime.runs.time.time", return_value=100):
        terminal, _ = await runs.create("alice", "digest", "key")
        active, _ = await runs.create("bob", "digest", "key")
        await runs.finish(terminal.id, "alice", ExecutionResult(success=False))
    with patch("gryphon.runtime.runs.time.time", return_value=200):
        assert await runs.get(terminal.id, "alice") is None
        assert await runs.get(active.id, "bob") is not None
        replacement, new = await runs.create("alice", "new digest", "key")
        assert new and replacement.id != terminal.id


@pytest.mark.parametrize("status", ["running", "unknown", "cancelled", ""])
async def test_run_invalid_finish_status_rejected(runs: RunStore, status: str) -> None:
    """A successful result may only be persisted with succeeded status."""
    record, _ = await runs.create("alice", "digest")
    with pytest.raises(InputValidationError):
        await runs.finish(record.id, "alice", ExecutionResult(success=True), status=status)


async def test_run_result_storage_has_a_hard_size_bound(runs: RunStore) -> None:
    """Oversized results must be placed in artifact storage by the executor."""
    record, _ = await runs.create("alice", "digest")
    with pytest.raises(InputValidationError, match="storage limit"):
        await runs.finish(record.id, "alice", ExecutionResult(success=True, data="x" * (2 * 1024 * 1024)))


async def test_run_storage_has_no_raw_key_code_or_input_fields(runs: RunStore) -> None:
    """The ledger stores a key digest and public receipt rather than raw requests."""
    await runs.create("alice", "digest", "caller-opaque-value")
    assert runs._db is not None
    async with runs._db.execute("SELECT * FROM runs") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert {"code", "inputs", "idempotency_key"}.isdisjoint(row.keys())
    assert "caller-opaque-value" not in row


async def test_run_database_uses_wal_full_and_reuses_connection(runs: RunStore) -> None:
    """Durability settings and pooled access are explicit."""
    assert runs._db is not None
    for pragma, expected in (("journal_mode", "wal"), ("synchronous", 2)):
        async with runs._db.execute(f"PRAGMA {pragma}") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == expected
    with patch("aiosqlite.connect", side_effect=AssertionError("unexpected connection")):
        record, _ = await runs.create("alice", "digest")
        await runs.start(record.id, "alice")
        await runs.finish(record.id, "alice", ExecutionResult(success=True))
        assert await runs.get(record.id, "alice") is not None


async def test_run_database_errors_are_sanitized(runs: RunStore) -> None:
    """Storage failures never echo database paths or request details."""
    assert runs._db is not None
    with (
        patch.object(runs._db, "execute", new=AsyncMock(side_effect=aiosqlite.Error("private value"))),
        pytest.raises(CacheError, match="^Failed to create run$"),
    ):
        await runs.create("alice", "digest")


async def test_run_transaction_rollback_releases_idempotency(runs: RunStore) -> None:
    """A cancelled transaction cannot leave an uncommitted claim on the pool."""
    with pytest.raises(asyncio.CancelledError):
        async with runs._transaction("test cancellation") as db:
            await db.execute("INSERT INTO runs VALUES ('id', 'alice', 'digest', NULL, 'queued', 1, 1, NULL)")
            raise asyncio.CancelledError
    assert await runs.get("id", "alice") is None


@pytest.mark.parametrize("owner,digest,key", [("", "digest", None), ("alice", "", None), ("alice", "digest", "")])
async def test_run_empty_identity_is_rejected(runs: RunStore, owner: str, digest: str, key: str | None) -> None:
    """Namespace and claim identity components cannot be empty."""
    with pytest.raises(InputValidationError):
        await runs.create(owner, digest, key)


@pytest.mark.parametrize("ttl,capacity", [(0, 1), (1, 0)])
def test_run_invalid_retention_rejected(tmp_path: Path, ttl: int, capacity: int) -> None:
    """Retention cannot configure an unbounded run ledger."""
    with pytest.raises(InputValidationError, match="retention"):
        RunStore(str(tmp_path / "invalid.db"), ttl_seconds=ttl, max_entries=capacity)


async def test_run_unserializable_result_is_sanitized(runs: RunStore) -> None:
    """Unsupported result values fail without leaking their representation."""
    record, _ = await runs.create("alice", "digest")
    with pytest.raises(InputValidationError, match="^Invalid run result$"):
        await runs.finish(record.id, "alice", ExecutionResult(success=True, data={"value": object()}))
